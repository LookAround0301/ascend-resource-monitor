"""Opt-in, persistent container enforcement for NPU jobs with unmapped IDs."""
import copy
import json
import os
from pathlib import Path
import re
import signal
import select
import threading
import time
import urllib.request

from control_common import CONTAINER_ID, ControlError
from process_exporter import employee_name_for, parse_stat, CONTAINER

EMPLOYEE = re.compile(r'[a-z][0-9]{8}\Z')
LABEL = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*")')


def current_process(row):
    try:
        proc = Path('/proc') / str(row['pid'])
        ticks = parse_stat((proc/'stat').read_text())['start_ticks']
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()[:12]
        cgroup = (proc/'cgroup').read_text()
        match = CONTAINER.search(cgroup)
        container = row.get('container_id', '')
        return (row['process_key'] == f'{boot}:{row["pid"]}:{ticks}'
                and ((container and match is not None and match.group(1) == container)
                     or (not container and match is None and 'kubepods' not in cgroup)))
    except (OSError, ValueError, KeyError, IndexError): return False


def load_candidates(config):
    """Fail closed on absent/invalid roster, incomplete or stale inventory."""
    mapping = json.loads(Path(config['owners_file']).read_text(encoding='utf-8'))
    names = mapping.get('employee_names')
    if not isinstance(names, dict) or not names or any(
            not EMPLOYEE.fullmatch(k) or not employee_name_for(k, mapping) for k in names):
        raise ValueError('Invalid employee roster')
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(config['process_url'], timeout=3) as response:
        raw = response.read(8 * 1024 * 1024).decode()
    prefix = config.get('metric_prefix', 'ascend_process_')
    scalars = {}
    rows = []
    for line in raw.splitlines():
        if line.startswith(prefix) and '{' not in line:
            name, value = line.split()
            scalars[name[len(prefix):]] = float(value)
        elif line.startswith(prefix + 'start_time_seconds{'):
            labels = {k: json.loads(v) for k, v in LABEL.findall(line)}
            target = labels.get('control_ref', '').split('@', 1)
            if len(target) != 2 or not CONTAINER_ID.fullmatch(target[0]): continue
            employee = labels.get('employee_id', '').strip().lower()
            if not EMPLOYEE.fullmatch(employee) or employee_name_for(employee, mapping): continue
            rows.append({'container_id': target[0], 'started_at': target[1],
                         'container': labels.get('container', ''), 'employee_id': employee,
                         'pid': int(labels['pid']), 'process_key': labels.get('process_key', '')})
    age = time.time() - scalars.get('inventory_timestamp_seconds', 0)
    if (scalars.get('inventory_success') != 1 or not 0 <= age <= 30
            or scalars.get('inventory_truncated') != 0
            or not 0 <= scalars.get('container_metadata_age_seconds', 1e9) <= 45):
        raise ValueError('Stale or incomplete process inventory')
    return rows


def load_unknown_candidates(config):
    """Read only explicit unknown owners from a fresh, complete NPU inventory."""
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(config['process_url'], timeout=3) as response:
        raw = response.read(8 * 1024 * 1024).decode()
    prefix = config.get('metric_prefix', 'ascend_process_')
    scalars, rows = {}, []
    for line in raw.splitlines():
        if line.startswith(prefix) and '{' not in line:
            name, value = line.split(); scalars[name[len(prefix):]] = float(value)
        elif line.startswith(prefix + 'start_time_seconds{'):
            labels = {k: json.loads(v) for k, v in LABEL.findall(line)}
            if labels.get('employee_id') != '未知' or labels.get('attribution') != '无可用工号线索': continue
            target = labels.get('control_ref', '')
            if target:
                parts = target.split('@', 1)
                if len(parts) != 2 or not CONTAINER_ID.fullmatch(parts[0]) or not parts[1]: continue
                cid, started = parts
            elif not labels.get('container'):
                cid, started = '', ''
            else: continue
            pid = int(labels['pid'])
            if pid <= 1: continue
            rows.append({'container_id':cid, 'started_at':started, 'container':labels.get('container',''),
                         'pid':pid, 'process_key':labels.get('process_key','')})
    age = time.time() - scalars.get('inventory_timestamp_seconds', 0)
    if (scalars.get('inventory_success') != 1 or not 0 <= age <= 30
            or scalars.get('inventory_truncated') != 0
            or not 0 <= scalars.get('container_metadata_age_seconds', 1e9) <= 45):
        raise ValueError('Stale or incomplete process inventory')
    return rows


class ContainerGuard:
    def __init__(self, engine, candidates=None, identity_check=None):
        self.engine = engine
        self.config = engine.config['guard']
        self.path = Path(self.config['state_file'])
        # Provision this explicitly. Losing the state must never clear blocks.
        self.state = json.loads(self.path.read_text(encoding='utf-8'))
        if self.state.get('version') != 1 or not isinstance(self.state.get('entries'), dict):
            raise ValueError('Invalid container guard state')
        if 'enabled' in self.state and type(self.state['enabled']) is not bool:
            raise ValueError('Invalid container guard enabled state')
        for cid, entry in self.state['entries'].items():
            if not CONTAINER_ID.fullmatch(cid) or not isinstance(entry, dict) or type(entry.get('whitelisted')) is not bool:
                raise ValueError('Invalid container guard entry')
        self.candidates = candidates or (lambda: load_candidates(self.config))
        self.identity_check = identity_check or current_process
        self.error = ''
        self.last_scan = 0
        self.next_discovery = 0
        self.unknown_candidates = lambda: load_unknown_candidates(self.config)
        self.unknown_terminated = {}
        self.stopped = threading.Event()

    def save(self):
        temporary = self.path.with_suffix('.next')
        fd = os.open(str(temporary), os.O_CREAT | os.O_TRUNC | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, ensure_ascii=False)
                f.flush(); os.fsync(f.fileno())
            os.replace(temporary, self.path)
            if os.name != 'nt':
                d = os.open(str(self.path.parent), os.O_DIRECTORY)
                try: os.fsync(d)
                finally: os.close(d)
        except Exception:
            # Reload the authoritative state on the next process start; do not
            # continue enforcing after an uncertain state write.
            self.stopped.set()
            raise

    def protected(self, info):
        labels = (info.get('Config') or {}).get('Labels') or {}
        return (info.get('Name', '').lstrip('/') in self.engine.config.get('protected_containers', [])
                or any(k.startswith('io.kubernetes.') for k in labels))

    def entries(self):
        return self.state['entries']

    def enabled(self):
        # Legacy installations retain their explicit configuration until switched.
        return self.state.get('enabled', self.config.get('enabled') is True)

    def policy(self, cid):
        entry = self.entries().get(cid)
        if not entry: return '未标记'
        return '白名单' if entry['whitelisted'] or entry.get('personnel_whitelisted') else ('持续拦截' if entry.get('marked_at') else '未标记')

    def personnel_exempt(self, cid, entry):
        allowed = False
        ids = entry.get('employee_ids', [])
        if ids and self.config.get('owners_file'):
            mapping = json.loads(Path(self.config['owners_file']).read_text(encoding='utf-8'))
            names = mapping.get('employee_names')
            if not isinstance(names, dict) or not names or any(
                    not EMPLOYEE.fullmatch(k) or not employee_name_for(k, mapping) for k in names):
                raise ValueError('Cannot verify personnel allowlist')
            allowed = all(isinstance(e, str) and EMPLOYEE.fullmatch(e) and employee_name_for(e, mapping) for e in ids)
        if bool(entry.get('personnel_whitelisted')) != bool(allowed):
            self.engine.write_audit({'action': 'container.guard.personnel-exemption',
                                     'node': self.engine.config['node'], 'container_id': cid,
                                     'employee_ids': ids, 'allowed': bool(allowed)})
            entry['personnel_whitelisted'] = bool(allowed)
            entry['last_result'] = '人员白名单，跳过自动拦截' if allowed else '已移出人员白名单'
            self.save()
        return bool(allowed)

    def snapshot(self):
        with self.engine.lock:
            return {'node': self.engine.config['node'], 'enabled': self.enabled(),
                    'last_scan': self.last_scan, 'error': self.error,
                    'entries': [{'container_id': cid, **copy.deepcopy(entry), 'policy': self.policy(cid)}
                                for cid, entry in self.entries().items()]}

    def set_enabled(self, data, actor):
        if (not isinstance(data, dict) or set(data) != {'node', 'enabled', 'confirmed'}
                or data.get('node') != self.engine.config['node']
                or type(data.get('enabled')) is not bool or data.get('confirmed') is not True):
            raise ControlError(400, '无效自动容器管控开关请求')
        if not isinstance(actor, dict) or not actor.get('id') or not actor.get('login'):
            raise ControlError(403, '缺少经认证的管理员')
        with self.engine.lock:
            if self.stopped.is_set(): raise ControlError(503, '自动管控状态保存失败，请检查服务')
            if data['enabled']:
                from personnel_roster import RosterStore
                RosterStore(self.engine).read()
            if self.enabled() == data['enabled']:
                return {'node': data['node'], 'enabled': self.enabled(), 'changed': False}
            event = {'action': 'container.guard.switch', 'node': data['node'], 'actor': actor, 'enabled': data['enabled']}
            self.engine.write_audit({**event, 'result': 'requested'})
            self.state['enabled'] = data['enabled']
            self.save()
            self.engine.write_audit({**event, 'result': 'saved'})
            if data['enabled']: self.next_discovery = 0
            return {'node': data['node'], 'enabled': self.enabled(), 'changed': True}

    def whitelist(self, data, actor):
        if (not isinstance(data, dict) or set(data) != {'node', 'container_id', 'allowed', 'confirmed'}
                or data.get('node') != self.engine.config['node']
                or not isinstance(data.get('container_id'), str) or not CONTAINER_ID.fullmatch(data['container_id'])
                or type(data.get('allowed')) is not bool or data.get('confirmed') is not True):
            raise ControlError(400, '无效白名单请求')
        if not isinstance(actor, dict) or not actor.get('id') or not actor.get('login'):
            raise ControlError(403, '缺少经认证的管理员')
        cid = data['container_id']
        with self.engine.lock:
            entry = self.entries().get(cid)
            if entry is None:
                info = self.engine.docker.inspect(cid)
                if info.get('Id') != cid: raise ControlError(409, '容器身份不匹配')
                entry = {'container': info.get('Name', '').lstrip('/'), 'employee_ids': [],
                         'marked_at': None, 'whitelisted': False, 'last_result': '尚未自动标记'}
                self.entries()[cid] = entry
            event = {'action': 'container.whitelist', 'node': data['node'], 'container_id': cid,
                     'actor': actor, 'allowed': data['allowed']}
            self.engine.write_audit({**event, 'result': 'requested'})
            entry['whitelisted'] = data['allowed']
            entry['updated_by'] = actor['login']
            entry['updated_at'] = time.time()
            self.save()
            self.engine.write_audit({**event, 'result': 'saved'})
            return {'message': '已加入白名单；不会自动启动容器' if data['allowed'] else '已移出白名单；已标记容器将恢复持续拦截',
                    'policy': self.policy(cid)}

    def discover(self):
        rows = self.candidates()  # never convert a source failure into unknown names
        self.error = ''
        if not self.enabled(): return
        groups = {}
        for row in rows: groups.setdefault(row['container_id'], []).append(row)
        devices = None
        for cid, members in groups.items():
            if cid in self.entries() and (self.entries()[cid]['whitelisted'] or self.entries()[cid].get('marked_at')): continue
            info = self.engine.docker.inspect(cid)
            if info.get('Id') != cid or self.protected(info): continue
            state = info.get('State', {})
            if not state.get('Running') or any(row['started_at'] != state.get('StartedAt') for row in members): continue
            if devices is None: devices = self.engine.get_npu()
            matching = sorted(devices & self.engine.docker.pids(cid) & {row['pid'] for row in members if self.identity_check(row)})
            if not matching: continue
            ids = sorted({row['employee_id'] for row in members if row['pid'] in matching})
            self.engine.write_audit({'action': 'container.guard.mark', 'node': self.engine.config['node'],
                                     'container_id': cid, 'employee_ids': ids, 'npu_pids': matching,
                                     'reason': 'employee-id-without-roster-name', 'result': 'requested'})
            self.entries()[cid] = {'container': info.get('Name', '').lstrip('/'), 'employee_ids': ids,
                                   'marked_at': time.time(), 'whitelisted': False, 'last_result': '已标记'}
            self.save()

    def enforce(self, cid, entry):
        if entry['whitelisted'] or not entry.get('marked_at'): return
        if self.personnel_exempt(cid, entry): return
        try: info = self.engine.docker.inspect(cid)
        except ControlError as e:
            if e.status == 404:
                entry['observed'] = '已删除'; return
            raise
        if info.get('Id') != cid: raise ControlError(409, '容器身份不匹配')
        entry['container'] = info.get('Name', '').lstrip('/')
        state = info.get('State', {})
        entry['observed'] = '运行中' if state.get('Running') else '已停止'
        if not state.get('Running') or self.protected(info): return
        started = state.get('StartedAt')
        if not isinstance(started, str) or not started: return
        if entry.get('attempted_start') == started: return  # reconcile uncertain results, never blind-retry
        current = self.engine.docker.inspect(cid)
        if current.get('Id') != cid or self.protected(current) or current.get('State', {}).get('StartedAt') != started or not current.get('State', {}).get('Running'): return
        event = {'action': 'container.guard.stop', 'node': self.engine.config['node'],
                 'container_id': cid, 'container': entry['container'], 'started_at': started,
                 'actor': {'id': 'container-guard', 'login': 'container-guard'}, 'reason': 'persistent-block'}
        self.engine.write_audit({**event, 'result': 'requested'})
        entry.update(attempted_start=started, last_result='停止结果待核实', attempted_at=time.time())
        self.save()  # durable intent before Docker, also across daemon restarts
        try:
            self.engine.docker.stop(cid)
            try:
                stopped = not self.engine.docker.inspect(cid).get('State', {}).get('Running')
            except ControlError as e:
                if e.status != 404: raise
                stopped = True
            entry['last_result'] = '已自动停止' if stopped else '停止结果待核实'
            self.engine.write_audit({**event, 'result': 'stopped' if stopped else 'state-changed'})
        except Exception:
            self.engine.write_audit({**event, 'result': 'uncertain'})
        self.save()

    def scan(self):
        with self.engine.lock:
            if self.stopped.is_set(): raise RuntimeError('Guard state writes failed')
            if time.monotonic() >= self.next_discovery:
                self.next_discovery = time.monotonic() + 10
                try:
                    self.discover()
                    if self.enabled() and self.config.get('unknown_npu_processes') is True:
                        self.enforce_unknown()
                except Exception: self.error = '新增标记已暂停：姓名表、采集数据或容器状态核验失败'
            if self.enabled():
                for cid, entry in list(self.entries().items()):
                    try: self.enforce(cid, entry)
                    except Exception: self.error = '部分容器核验或审计失败；请检查服务日志'
            self.last_scan = time.time()

    def enforce_unknown(self):
        if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
            raise RuntimeError('Safe PID handles unavailable')
        rows = self.unknown_candidates()
        npu = self.engine.get_npu()
        seen = set()
        for row in rows:
            cid, pid, key = row['container_id'], row['pid'], row['process_key']
            if not key or pid not in npu: continue
            if cid:
                entry = self.entries().get(cid, {})
                if entry.get('whitelisted') or entry.get('personnel_whitelisted'): continue
                info = self.engine.docker.inspect(cid)
                if (info.get('Id') != cid or self.protected(info)
                        or not info.get('State', {}).get('Running')
                        or info['State'].get('StartedAt') != row['started_at']
                        or pid not in self.engine.docker.pids(cid)): continue
            if not self.identity_check(row): continue
            seen.add(key)
            prior = self.unknown_terminated.get(key)
            if prior is not None and time.monotonic() - prior < 5: continue
            try: fd = os.pidfd_open(pid)
            except ProcessLookupError: continue
            try:
                if not self.identity_check(row): continue
                poll = select.poll(); poll.register(fd, select.POLLIN)
                if poll.poll(0): continue
                sig = signal.SIGTERM if prior is None else signal.SIGKILL
                event = {'action':'process.guard.stop', 'node':self.engine.config['node'],
                         'container_id':cid, 'pid':pid, 'process_key':key,
                         'reason':'no-employee-evidence', 'signal':sig.name}
                self.engine.write_audit({**event, 'result':'requested'})
                try: signal.pidfd_send_signal(fd, sig)
                except ProcessLookupError: continue
                self.unknown_terminated[key] = time.monotonic()
                self.engine.write_audit({**event, 'result':'signal-sent'})
            finally: os.close(fd)
        self.unknown_terminated = {k:v for k,v in self.unknown_terminated.items() if k in seen}

    def run(self):
        while not self.stopped.is_set():
            try: self.scan()
            except Exception: self.error = '自动管控已暂停：状态保存失败'
            self.stopped.wait(max(1, self.config.get('interval', 2)))

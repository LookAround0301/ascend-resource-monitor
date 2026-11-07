"""Validation and a narrow Docker stop capability; no shell commands are accepted."""
import http.client
import json
import os
from pathlib import Path
import re
import socket
import threading
import time
import urllib.request

CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")
NODE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
STARTED = re.compile(r"[0-9TZ:.+-]{10,64}\Z")


class ControlError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def validate_request(data):
    if not isinstance(data, dict) or set(data) != {'node', 'container_id', 'container_started_at', 'confirmed'}:
        raise ControlError(400, '停止请求字段不完整或不受支持')
    if not isinstance(data['node'], str) or not NODE.fullmatch(data['node']):
        raise ControlError(400, '无效主机')
    if not isinstance(data['container_id'], str) or not CONTAINER_ID.fullmatch(data['container_id']):
        raise ControlError(400, '没有可操作的完整容器 ID，请刷新面板')
    if not isinstance(data['container_started_at'], str) or not STARTED.fullmatch(data['container_started_at']):
        raise ControlError(400, '缺少容器启动标识，请刷新面板')
    if data['confirmed'] is not True:
        raise ControlError(400, '请先确认停止操作')
    return data


def parse_browser_payload(data):
    if not isinstance(data, dict) or set(data) != {'node','target','confirmed'}:
        raise ControlError(400,'无效停止请求')
    target=data.get('target')
    if not isinstance(target,str) or target.count('@')!=1:
        raise ControlError(400,'该行没有可操作的容器，请刷新面板')
    cid,started=target.split('@',1)
    return validate_request({'node':data['node'],'container_id':cid,
                             'container_started_at':started,'confirmed':data['confirmed']})


def is_admin(user, orgs, org_id):
    if not isinstance(user, dict) or not user.get('id') or user.get('isDisabled'):
        return False
    if user.get('isGrafanaAdmin') is True:
        return True
    return user.get('orgId') == org_id and isinstance(orgs, list) and any(
        x.get('orgId') == org_id and x.get('role') == 'Admin' for x in orgs if isinstance(x, dict))


def check_browser_request(headers, host, scheme):
    if headers.get('X-A5-Control') != 'stop-v1':
        raise ControlError(403, '请求缺少操作校验标识')
    if headers.get('Origin') != scheme + '://' + host:
        raise ControlError(403, '拒绝跨站停止请求')
    if headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
        raise ControlError(415, '请求必须为 JSON')


def audit(path, event):
    """An unavailable audit log blocks mutations rather than silently losing evidence."""
    row = {'timestamp': time.time(), **event}
    flags = os.O_CREAT | os.O_WRONLY | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(str(path), flags, 0o600)
    try:
        line = (json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=45):
        super().__init__('localhost', timeout=timeout)
        self.socket_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class Docker:
    def __init__(self, path='/var/run/docker.sock'):
        self.path = path

    def call(self, method, path):
        connection = UnixConnection(self.path)
        try:
            connection.request(method, '/v1.39' + path, body=b'' if method == 'POST' else None)
            response = connection.getresponse()
            body = response.read(2 * 1024 * 1024)
            if response.status >= 400:
                raise ControlError(response.status, 'Docker 拒绝操作：HTTP ' + str(response.status))
            return json.loads(body) if body else {}
        finally:
            connection.close()

    def inspect(self, cid):
        return self.call('GET', '/containers/' + cid + '/json')

    def pids(self, cid):
        data = self.call('GET', '/containers/' + cid + '/top?ps_args=-eo%20pid')
        if 'PID' not in data.get('Titles', []):
            raise ControlError(409, '无法核实容器进程')
        index = data['Titles'].index('PID')
        return {int(row[index]) for row in data['Processes'] if row[index].isdigit()}

    def stop(self, cid):
        self.call('POST', '/containers/' + cid + '/stop?t=30')

    def kill(self, cid):
        self.call('POST', '/containers/' + cid + '/kill?signal=SIGKILL')


def npu_pids(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=5) as response:
            text = response.read(4 * 1024 * 1024).decode()
    except Exception as e:
        raise ControlError(503, '无法核实当前 NPU 进程，停止操作已拒绝') from e
    records = [line for line in text.splitlines() if line.startswith('npu_chip_info_process_info')]
    if not any(line.startswith('npu_chip_info_process_info_num{') for line in records):
        raise ControlError(503, 'NPU 进程指标不可用')
    times = [float(line.split()[-1]) / 1000 for line in records if len(line.rsplit('}', 1)[-1].split()) == 2]
    if times and time.time() - max(times) > 30:
        raise ControlError(503, 'NPU 进程指标已过期')
    found = set()
    for line in records:
        if line.startswith('npu_chip_info_process_info{'):
            match = re.search(r'(?:[{,])process_id="([0-9]+)"', line)
            if match:
                found.add(int(match.group(1)))
    return found


class StopEngine:
    def __init__(self, config, docker=None, get_npu=None, write_audit=None):
        self.config = config
        self.docker = docker or Docker(config.get('docker_socket', '/var/run/docker.sock'))
        self.get_npu = get_npu or (lambda: npu_pids(config['npu_url']))
        self.write_audit = write_audit or (lambda event: audit(config['audit_log'], event))
        self.lock = threading.Lock()

    def describe(self, request):
        """Read-only confirmation metadata, checked against the exact instance."""
        data = validate_request(request)
        if data['node'] != self.config['node']:
            raise ControlError(400, '目标主机不匹配')
        info = self.docker.inspect(data['container_id'])
        if info.get('Id') != data['container_id']:
            raise ControlError(409, '容器身份不匹配')
        state = info.get('State', {})
        if not state.get('Running') or state.get('StartedAt') != data['container_started_at']:
            raise ControlError(409, '容器已停止或重新启动，请刷新面板')
        return {'node':data['node'],'container_id':info['Id'],
                'container':info.get('Name','').lstrip('/'),
                'container_started_at':state['StartedAt']}

    def stop(self, request, actor):
        data = validate_request(request)
        if data['node'] != self.config['node']:
            raise ControlError(400, '目标主机不匹配')
        if not isinstance(actor, dict) or not actor.get('id') or not actor.get('login'):
            raise ControlError(403, '缺少经认证的操作人')
        cid = data['container_id']
        with self.lock:
            info = self.docker.inspect(cid)
            if info.get('Id') != cid:
                raise ControlError(409, '容器身份不匹配')
            state = info.get('State', {})
            if not state.get('Running'):
                raise ControlError(409, '容器已经停止，请刷新面板')
            if state.get('StartedAt') != data['container_started_at']:
                raise ControlError(409, '容器已重新启动，请刷新后重新确认')
            name = info.get('Name', '').lstrip('/')
            labels = (info.get('Config') or {}).get('Labels') or {}
            if any(k.startswith('io.kubernetes.') for k in labels):
                raise ControlError(409, '此容器由 Kubernetes 管理，请通过工作负载管理平台停止')
            if name in self.config.get('protected_containers', []):
                raise ControlError(403, '该容器受到保护')
            device_pids = self.get_npu()
            matching = sorted(device_pids & self.docker.pids(cid))
            if not matching:
                raise ControlError(409, '该容器当前没有 NPU 进程，请刷新面板')
            current = self.docker.inspect(cid)
            if current.get('State', {}).get('StartedAt') != data['container_started_at']:
                raise ControlError(409, '容器状态已改变，请重新确认')
            event = {'action': 'container.stop', 'node': data['node'], 'container_id': cid,
                     'container': name, 'started_at': data['container_started_at'],
                     'actor': actor, 'npu_pids': matching}
            self.write_audit({**event, 'result': 'requested'})
            try:
                self.docker.stop(cid)
            except Exception:
                self.write_audit({**event, 'result': 'uncertain'})
                raise ControlError(504, '停止请求结果暂不确定，请刷新容器状态后核实，不要连续重试')
            try:
                stopped = not self.docker.inspect(cid).get('State', {}).get('Running')
            except ControlError as e:
                if e.status != 404: raise
                stopped = True  # Docker --rm containers disappear after a successful stop.
            self.write_audit({**event, 'result': 'stopped' if stopped else 'state-changed'})
            if not stopped:
                raise ControlError(409, '容器状态发生变化，请刷新后核实')
            return {'message': '容器 ' + name + ' 已停止', 'node': data['node'], 'container_id': cid, 'stopped': True}

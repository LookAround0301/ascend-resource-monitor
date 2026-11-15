import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'assets/runtime'))
from container_guard import ContainerGuard, load_candidates, load_unknown_candidates
from control_common import ControlError, StopEngine
from process_exporter import guard_display_start, guard_metrics, metric_lines
from patch_dashboard import add_guard_panel

CID = 'a' * 64
START = '2026-11-03T00:00:00Z'
ACTOR = {'id': 1, 'login': 'test-admin'}


class Docker:
    def __init__(self):
        self.info = {'Id': CID, 'Name': '/test-container', 'State': {'Running': True, 'StartedAt': START}, 'Config': {'Labels': {}}}
        self.calls = []
        self.fail = False

    def inspect(self, cid): return copy.deepcopy(self.info)
    def pids(self, cid): return {23}
    def stop(self, cid):
        self.calls.append(cid)
        if self.fail: raise TimeoutError()
        self.info['State']['Running'] = False


class GuardTests(unittest.TestCase):
    def test_custom_metric_prefix_preserves_guard_records_and_label_values(self):
        lines=['# TYPE ascend_container_guard_info gauge',
               'ascend_container_guard_info{container="ascend_keep",employee_ids="j30064796",guard_state="持续拦截"} 1',
               'ascend_process_inventory_success 1']
        renamed=metric_lines(lines,'a5')
        self.assertEqual(renamed[0],'# TYPE a5_container_guard_info gauge')
        self.assertEqual(renamed[1],'a5_container_guard_info{container="ascend_keep",employee_ids="j30064796",guard_state="持续拦截"} 1')
        self.assertEqual(renamed[2],'a5_process_inventory_success 1')
        self.assertEqual(metric_lines(lines),lines)
        with self.assertRaises(ValueError): metric_lines(lines,'bad-prefix')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.json'
        self.path.write_text('{"version":1,"entries":{}}')
        self.config = {'node': 'test', 'guard': {'enabled': True, 'state_file': str(self.path)}}
        self.events = []; self.docker = Docker()
        self.engine = StopEngine(self.config, self.docker, lambda: {23}, self.events.append)
        self.rows = [{'container_id': CID, 'container': 'test-container', 'started_at': START, 'pid': 23, 'employee_id': 'z12345678'}]
        self.guard = ContainerGuard(self.engine, lambda: self.rows, lambda row: True)

    def allow(self, enabled):
        return self.guard.whitelist({'node': 'test', 'container_id': CID, 'allowed': enabled, 'confirmed': True}, ACTOR)

    def test_switch_defaults_off_and_preserves_legacy_explicit_enable(self):
        config = copy.deepcopy(self.config); config['guard'].pop('enabled')
        engine = StopEngine(config, self.docker, lambda: {23}, self.events.append)
        guard = ContainerGuard(engine, lambda: self.rows, lambda row: True)
        self.assertFalse(guard.snapshot()['enabled']); guard.scan(); self.assertFalse(self.docker.calls)
        self.assertTrue(self.guard.snapshot()['enabled'])
        self.guard.set_enabled({'node':'test','enabled':False,'confirmed':True}, ACTOR)
        reloaded = ContainerGuard(self.engine, lambda: self.rows, lambda row: True)
        self.assertFalse(reloaded.snapshot()['enabled']); reloaded.scan(); self.assertFalse(self.docker.calls)
        owners=Path(self.temp.name)/'owners.json'
        owners.write_text(json.dumps({'employee_names':{'a12345678':'Known'}}), encoding='utf-8')
        self.config['guard']['owners_file']=str(owners)
        reloaded.set_enabled({'node':'test','enabled':True,'confirmed':True}, ACTOR)
        self.assertTrue(ContainerGuard(self.engine,lambda: self.rows,lambda row: True).snapshot()['enabled'])
        reloaded.scan();self.assertEqual(self.docker.calls,[CID])

    def test_switch_requires_admin_confirmation_and_valid_roster_before_enabling(self):
        self.guard.set_enabled({'node':'test','enabled':False,'confirmed':True}, ACTOR)
        data={'node':'test','enabled':True,'confirmed':True}
        for change in [{'node':'wrong'},{'enabled':1},{'confirmed':False}]:
            with self.assertRaises(ControlError):self.guard.set_enabled({**data,**change}, ACTOR)
        with self.assertRaises(ControlError):self.guard.set_enabled(data,{})
        owners=Path(self.temp.name)/'owners.json';owners.write_text('{"employee_names":{}}')
        self.config['guard']['owners_file']=str(owners)
        with self.assertRaises(ControlError):self.guard.set_enabled(data,ACTOR)
        self.guard.scan();self.assertFalse(self.docker.calls)

    def test_disabling_preserves_existing_mark_without_stopping_on_restart(self):
        self.guard.scan();self.assertEqual(self.docker.calls,[CID])
        self.guard.set_enabled({'node':'test','enabled':False,'confirmed':True},ACTOR)
        self.docker.info['State']={'Running':True,'StartedAt':'2026-11-03T02:00:00Z'}
        reloaded=ContainerGuard(self.engine,lambda: [],lambda row: True)
        reloaded.scan();self.assertEqual(self.docker.calls,[CID]);self.assertTrue(reloaded.entries()[CID]['marked_at'])
        owners=Path(self.temp.name)/'owners.json'
        owners.write_text(json.dumps({'employee_names':{'a12345678':'Known'}}),encoding='utf-8')
        self.config['guard']['owners_file']=str(owners)
        reloaded.set_enabled({'node':'test','enabled':True,'confirmed':True},ACTOR)
        reloaded.scan();self.assertEqual(self.docker.calls,[CID,CID])

    def test_mark_stop_restart_and_rename_without_npu(self):
        self.guard.scan()
        self.assertEqual(self.docker.calls, [CID])
        self.assertEqual(self.guard.policy(CID), '持续拦截')
        self.rows.clear()
        self.engine.get_npu = lambda: set()
        self.guard = ContainerGuard(self.engine, lambda: self.rows, lambda row: True)
        self.docker.info.update(Name='/renamed')
        self.docker.info['State'] = {'Running': True, 'StartedAt': '2026-11-03T01:00:00Z'}
        self.guard.scan()
        self.assertEqual(self.docker.calls, [CID, CID])
        self.assertEqual(self.guard.entries()[CID]['container'], 'renamed')

    def test_whitelist_persists_and_removal_resumes_block(self):
        self.guard.scan(); self.allow(True)
        self.docker.info['State'] = {'Running': True, 'StartedAt': '2026-11-03T01:00:00Z'}
        self.guard = ContainerGuard(self.engine, lambda: [], lambda row: True)
        self.guard.scan()
        self.assertEqual(len(self.docker.calls), 1)
        self.assertEqual(self.guard.policy(CID), '白名单')
        self.allow(False); self.guard.scan()
        self.assertEqual(len(self.docker.calls), 2)

    def test_preemptive_whitelist_never_marks_or_stops(self):
        self.allow(True); self.guard.scan()
        self.assertFalse(self.docker.calls)
        self.assertIsNone(self.guard.entries()[CID]['marked_at'])
        self.allow(False); self.guard.next_discovery = 0; self.guard.scan()
        self.assertEqual(self.docker.calls, [CID])

    def test_personnel_allowlist_overrides_existing_mark_and_deletion_restores(self):
        owners=Path(self.temp.name)/'owners.json'
        owners.write_text(json.dumps({'employee_names':{'a12345678':'Existing'}}),encoding='utf-8')
        self.config['guard']['owners_file']=str(owners)
        self.guard.scan()
        self.assertEqual(len(self.docker.calls),1)
        owners.write_text(json.dumps({'employee_names':{'a12345678':'Existing','z12345678':'Allowed'}}),encoding='utf-8')
        self.docker.info['State']={'Running':True,'StartedAt':'2026-11-03T02:00:00Z'}
        self.guard.scan()
        self.assertEqual(len(self.docker.calls),1)
        self.assertEqual(self.guard.policy(CID),'白名单')
        self.assertIn('guard_state="白名单"','\n'.join(guard_metrics(self.path)))
        self.assertTrue(self.guard.entries()[CID]['marked_at'])
        owners.write_text('{broken',encoding='utf-8')
        self.guard.scan()
        self.assertEqual(len(self.docker.calls),1)
        owners.write_text(json.dumps({'employee_names':{'a12345678':'Existing'}}),encoding='utf-8')
        self.guard.scan()
        self.assertEqual(len(self.docker.calls),2)
        self.assertEqual(self.guard.policy(CID),'持续拦截')

    def test_disabled_stale_foreign_protected_and_non_npu_are_not_marked(self):
        variants = ['disabled', 'stale', 'foreign', 'protected', 'k8s', 'non_npu', 'reused_pid']
        for variant in variants:
            with self.subTest(variant=variant):
                docker = Docker(); config = copy.deepcopy(self.config); rows = copy.deepcopy(self.rows)
                if variant == 'disabled': config['guard']['enabled'] = False
                if variant == 'stale': rows[0]['started_at'] = 'old'
                if variant == 'foreign': docker.info['Id'] = 'b' * 64
                if variant == 'protected': config['protected_containers'] = ['test-container']
                if variant == 'k8s': docker.info['Config']['Labels'] = {'io.kubernetes.pod.name': 'test'}
                engine = StopEngine(config, docker, lambda: set() if variant == 'non_npu' else {23}, self.events.append)
                guard = ContainerGuard(engine, lambda: rows, lambda row: variant != 'reused_pid'); guard.scan()
                self.assertFalse(docker.calls); self.assertFalse(guard.entries())

    def test_failed_discovery_does_not_clear_existing_block(self):
        self.guard.scan()
        def fail(): raise ValueError()
        self.guard.candidates = fail; self.guard.next_discovery = 0
        self.docker.info['State'] = {'Running': True, 'StartedAt': '2026-11-03T01:00:00Z'}
        self.guard.scan()
        self.assertEqual(len(self.docker.calls), 2)
        self.assertTrue(self.guard.error)

    def test_uncertain_stop_intent_survives_restart_and_is_not_retried(self):
        self.docker.fail = True
        self.guard.scan(); self.guard = ContainerGuard(self.engine, lambda: self.rows, lambda row: True)
        self.guard.scan(); self.guard.scan()
        self.assertEqual(self.docker.calls, [CID])
        self.assertEqual(self.guard.entries()[CID]['last_result'], '停止结果待核实')

    def test_bad_audit_or_state_prevents_stop_and_missing_state_is_not_reset(self):
        def fail(*args): raise OSError()
        self.engine.write_audit = fail; self.guard.scan()
        self.assertFalse(self.docker.calls)
        self.engine.write_audit = self.events.append
        self.guard.next_discovery = 0
        with patch.object(self.guard, 'save', side_effect=fail): self.guard.scan()
        self.assertFalse(self.docker.calls)
        self.path.unlink()
        with self.assertRaises(FileNotFoundError): ContainerGuard(self.engine)

    def test_whitelist_input_requires_actor_exact_id_and_confirmation(self):
        data = {'node': 'test', 'container_id': CID, 'allowed': True, 'confirmed': True}
        for change in [{'node': 'elsewhere'}, {'container_id': 'test-container'}, {'allowed': 1}, {'confirmed': False}]:
            with self.assertRaises(ControlError): self.guard.whitelist({**data, **change}, ACTOR)
        with self.assertRaises(ControlError): self.guard.whitelist(data, {})
        self.assertFalse(self.guard.entries())

    def test_state_metrics_and_dashboard_keep_stopped_entry(self):
        self.guard.scan()
        self.assertIn('guard_state="持续拦截"', '\n'.join(guard_metrics(self.path)))
        self.allow(True)
        self.assertIn('guard_state="白名单"', '\n'.join(guard_metrics(self.path)))
        old = {'uid': 'test', 'panels': [{'id': 20, 'gridPos': {'y': 4}, 'title': 'original'}]}
        patched = add_guard_panel(old)
        self.assertIn('guard_state=~"持续拦截|未知工号进程拦截"',patched['panels'][-1]['targets'][0]['expr'])
        self.assertEqual(len(patched['panels']), 2)
        self.assertFalse(patched['panels'][-1]['options']['enablePagination'])
        self.assertEqual(patched['panels'][-1]['gridPos']['h'], 12)
        self.assertEqual(add_guard_panel(patched)['panels'], patched['panels'])
        self.assertEqual(old['panels'][0]['gridPos']['y'], 4)
        legacy = json.loads(json.dumps(patched))
        legacy['panels'][-1]['title'] = '容器管控 · 持续拦截与白名单'
        legacy['panels'][-1]['targets'][0]['expr'] = 'a5_container_guard_info{node=~"$node"}'
        updated = add_guard_panel(legacy, 'a5')
        self.assertEqual(len(updated['panels']),2)
        self.assertEqual(updated['panels'][-1]['targets'][0]['expr'],'a5_container_guard_info{node=~"$node",guard_state=~"持续拦截|未知工号进程拦截"}')

    def test_guard_scroll_layout_upgrades_existing_panel_once(self):
        original = {'panels': [
            {'id': 1, 'title': '容器管控 · 持续拦截',
             'gridPos': {'x': 0, 'y': 4, 'w': 24, 'h': 6},
             'options': {'enablePagination': True, 'cellHeight': 'sm'}},
            {'id': 2, 'title': '排队与资源分配',
             'gridPos': {'x': 0, 'y': 10, 'w': 24, 'h': 20}},
        ]}
        updated = add_guard_panel(original)
        self.assertFalse(updated['panels'][0]['options']['enablePagination'])
        self.assertEqual(updated['panels'][0]['options']['cellHeight'], 'sm')
        self.assertEqual(updated['panels'][0]['gridPos']['h'], 12)
        self.assertEqual(updated['panels'][1]['gridPos']['y'], 16)
        self.assertEqual(add_guard_panel(updated), updated)
        self.assertEqual(original['panels'][1]['gridPos']['y'], 10)
        updated['panels'][0]['gridPos']['h'] = 18
        updated['panels'][1]['gridPos']['y'] = 22
        reduced = add_guard_panel(updated)
        self.assertEqual(reduced['panels'][0]['gridPos']['h'], 12)
        self.assertEqual(reduced['panels'][1]['gridPos']['y'], 16)
        reference = {'id': 3, 'title': 'NPU 进程归属与工作目录',
                     'gridPos': {'x': 0, 'y': 42, 'w': 24, 'h': 10}}
        reduced['panels'].append(reference)
        matched = add_guard_panel(reduced)
        self.assertEqual(matched['panels'][0]['gridPos']['h'], 10)
        self.assertEqual(matched['panels'][1]['gridPos']['y'], 14)
        self.assertEqual(matched['panels'][2]['gridPos']['h'], 10)
        self.assertEqual(add_guard_panel(matched), matched)

    def test_daily_display_rolls_at_nine_beijing_without_clearing_marks(self):
        before = datetime(2026, 11, 15, 0, 59, 59, tzinfo=timezone.utc).timestamp()
        after = before + 1
        self.guard.state['display_reset'] = {'timezone': 'Asia/Shanghai', 'hour': 9}
        self.guard.scan()
        entry = self.guard.entries()[CID]
        with patch('container_guard.time.time', return_value=before):
            self.guard.record_display(entry, 'container', '已自动停止', completed=True)
            self.guard.save()
            self.assertIn('本期停止 1 次', '\n'.join(guard_metrics(self.path)))
        with patch('process_exporter.time.time', return_value=after):
            self.assertNotIn('container_id=', '\n'.join(guard_metrics(self.path)))
        self.assertTrue(entry['marked_at'])
        self.assertFalse(entry['whitelisted'])
        self.assertEqual(guard_display_start(self.guard.state, after), after)
        self.assertEqual(guard_display_start(self.guard.state, before), after - 86400)
        self.guard = ContainerGuard(self.engine, lambda: [], lambda row: True)
        self.docker.info['State'] = {'Running': True, 'StartedAt': 'new-start'}
        with patch('container_guard.time.time', return_value=after):
            self.guard.scan()
        self.assertEqual(len(self.docker.calls), 2)
        with patch('process_exporter.time.time', return_value=after):
            self.assertIn('本期停止 1 次', '\n'.join(guard_metrics(self.path)))

    def test_daily_unknown_count_resets_and_persists_across_restart(self):
        start = datetime(2026, 11, 15, 1, tzinfo=timezone.utc).timestamp()
        self.guard.state['display_reset'] = {'timezone': 'Asia/Shanghai', 'hour': 9}
        row = dict(container_id=CID, started_at=START, pid=23, process_key='boot:23:1')
        for now in (start + 1, start + 2, start + 86400):
            with patch('container_guard.time.time', return_value=now):
                self.guard.record_unknown_interception(row, self.docker.info, 'SIGTERM', '已发送', new_attempt=True)
        reloaded = ContainerGuard(self.engine, lambda: [], lambda row: True)
        entry = reloaded.entries()[CID]
        self.assertEqual(entry['unknown_interception']['signals_sent'], 3)
        self.assertEqual(entry['daily_interception']['process_signals'], 1)
        self.assertIn('本期发送 1 次', entry['daily_interception']['result'])
        self.assertIsNone(entry['marked_at'])
        with patch('process_exporter.time.time', return_value=start + 86400):
            self.assertIn('未知工号进程拦截', '\n'.join(guard_metrics(self.path)))
        with patch('process_exporter.time.time', return_value=start + 172800):
            self.assertNotIn('container_id=', '\n'.join(guard_metrics(self.path)))

    def test_daily_display_preserves_whitelist_exemption_and_validates_config(self):
        self.guard.state['display_reset'] = {'timezone': 'Asia/Shanghai', 'hour': 9}
        self.guard.scan()
        self.allow(True)
        self.assertIn('guard_state="白名单"', '\n'.join(guard_metrics(self.path)))
        for invalid in ({'timezone': 'Asia/Shanghai', 'hour': 24}, {'hour': 9}):
            self.guard.state['display_reset'] = invalid
            self.guard.save()
            self.assertEqual(guard_metrics(self.path), ['ascend_container_guard_state_read_success 0'])

    def test_reader_uses_real_roster_and_requires_complete_fresh_source(self):
        owners = Path(self.temp.name) / 'owners.json'
        owners.write_text(json.dumps({'employee_names': {'a12345678': 'Known'}}))
        config = {'owners_file': str(owners), 'process_url': 'http://127.0.0.1/metrics'}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, _): return raw.encode()
        base = ('ascend_process_inventory_success 1\nascend_process_inventory_truncated 0\n'
                'ascend_process_container_metadata_age_seconds 1\n'
                f'ascend_process_inventory_timestamp_seconds {time.time()}\n')
        raw = base + '\n'.join('ascend_process_start_time_seconds{employee_id="'+eid+'",employee_name="",pid="23",container="test",control_ref="'+CID+'@'+START+'"} 1' for eid in ['a12345678', 'z12345678', '未知'])
        with patch('container_guard.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = Response()
            rows = load_candidates(config)
            self.assertEqual([r['employee_id'] for r in rows], ['z12345678'])
            raw = raw.replace('inventory_truncated 0', 'inventory_truncated 1')
            with self.assertRaises(ValueError): load_candidates(config)
            raw = 'ascend_process_inventory_success 1'
            with self.assertRaises(ValueError): load_candidates(config)
            owners.write_text('{"employee_names":{}}')
            with self.assertRaises(ValueError): load_candidates(config)

    def test_unknown_reader_requires_explicit_unknown_and_fresh_complete_inventory(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, _): return raw.encode()
        config={'process_url':'http://127.0.0.1/metrics'}
        base=('ascend_process_inventory_success 1\nascend_process_inventory_truncated 0\n'
              'ascend_process_container_metadata_age_seconds 1\n'
              f'ascend_process_inventory_timestamp_seconds {time.time()}\n')
        def line(employee, attribution, pid='23', ref=CID+'@'+START, container='test'):
            labels={'employee_id':employee,'attribution':attribution,'pid':pid,
                    'control_ref':ref,'container':container,'process_key':'boot:'+pid+':12'}
            return 'ascend_process_start_time_seconds{'+','.join(k+'='+json.dumps(v,ensure_ascii=False) for k,v in labels.items())+'} 1'
        raw=base+'\n'.join([line('未知','无可用工号线索'),line('待确认','多个工号线索'),
                            line('a12345678','运行用户'),line('未知','无可用工号线索','24','','')])
        with patch('container_guard.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value=Response()
            rows=load_unknown_candidates(config)
            self.assertEqual([(r['pid'],r['container_id']) for r in rows],[(23,CID),(24,'')])
            raw=raw.replace('inventory_truncated 0','inventory_truncated 1')
            with self.assertRaises(ValueError):load_unknown_candidates(config)

    def test_unknown_process_termination_is_scoped_and_rechecks_identity(self):
        self.config['guard']['unknown_npu_processes']=True
        self.guard.unknown_candidates=lambda:[dict(container_id=CID,started_at=START,container='test-container',pid=23,process_key='boot:23:1')]
        class Poll:
            def register(self,*args):pass
            def poll(self,*args):return []
        with patch('container_guard.os.pidfd_open',return_value=77,create=True), \
             patch('container_guard.os.close'), patch('container_guard.select.poll',return_value=Poll(),create=True), \
             patch('container_guard.select.POLLIN',1,create=True), \
             patch('container_guard.signal.SIGKILL',SimpleNamespace(name='SIGKILL'),create=True), \
             patch('container_guard.signal.pidfd_send_signal',create=True) as send, \
             patch('container_guard.time.monotonic',return_value=100) as clock:
            self.guard.enforce_unknown()
            self.assertEqual(send.call_args.args[1].name,'SIGTERM')
            self.assertFalse(self.docker.calls)
            entry = json.loads(self.path.read_text())['entries'][CID]
            self.assertIsNone(entry['marked_at'])
            self.assertEqual(entry['unknown_interception']['pid'], 23)
            self.assertEqual(entry['unknown_interception']['signals_sent'], 1)
            self.assertEqual(self.guard.policy(CID), '未知工号进程拦截')
            self.assertIn('guard_state="未知工号进程拦截"', '\n'.join(guard_metrics(self.path)))
            self.assertIn('employee_ids="未知"', '\n'.join(guard_metrics(self.path)))
            self.guard.enforce_unknown();self.assertEqual(send.call_count,1)
            clock.return_value=106;self.guard.enforce_unknown()
            self.assertEqual(send.call_args.args[1].name,'SIGKILL')
            self.guard.identity_check=lambda row:False
            clock.return_value=120;self.guard.enforce_unknown();self.assertEqual(send.call_count,2)
            self.guard.identity_check=lambda row:True
            self.guard.entries()[CID]={'whitelisted':True}
            self.guard.enforce_unknown();self.assertEqual(send.call_count,2)
            self.guard.entries().clear();self.config['protected_containers']=['test-container']
            self.guard.enforce_unknown();self.assertEqual(send.call_count,2)

    def test_unknown_record_survives_restart_without_becoming_container_block(self):
        self.guard.record_unknown_interception(
            dict(container_id=CID, started_at=START, pid=23, process_key='boot:23:1'),
            self.docker.info, 'SIGTERM', '已发送', new_attempt=True,
        )
        reloaded = ContainerGuard(self.engine, lambda: [], lambda row: True)
        self.docker.info['State']['StartedAt'] = 'new-start'
        self.engine.get_npu = lambda: set()
        reloaded.scan()
        self.assertFalse(self.docker.calls)
        self.assertEqual(reloaded.policy(CID), '未知工号进程拦截')
        self.assertEqual(reloaded.entries()[CID]['unknown_interception']['signals_sent'], 1)
        reloaded.whitelist({'node':'test','container_id':CID,'allowed':True,'confirmed':True},ACTOR)
        self.assertEqual(reloaded.policy(CID), '白名单')
        reloaded.whitelist({'node':'test','container_id':CID,'allowed':False,'confirmed':True},ACTOR)
        self.assertEqual(reloaded.policy(CID), '未知工号进程拦截')
        reloaded.scan()
        self.assertFalse(self.docker.calls)

    def test_unknown_history_is_preserved_when_existing_roster_rule_marks_container(self):
        self.guard.record_unknown_interception(
            dict(container_id=CID, started_at=START, pid=23, process_key='boot:23:1'),
            self.docker.info, 'SIGTERM', '已发送', new_attempt=True,
        )
        self.guard.scan()
        self.assertEqual(self.guard.policy(CID), '持续拦截')
        self.assertEqual(self.docker.calls, [CID])
        self.assertEqual(self.guard.entries()[CID]['unknown_interception']['signals_sent'], 1)

    def test_failed_state_save_prevents_unknown_process_signal(self):
        row = dict(container_id=CID, started_at=START, pid=23, process_key='boot:23:1')
        self.guard.unknown_candidates = lambda: [row]
        with patch('container_guard.os.pidfd_open', return_value=77, create=True), \
             patch('container_guard.os.close'), patch('container_guard.select.poll', create=True) as poll, \
             patch('container_guard.select.POLLIN', 1, create=True), \
             patch('container_guard.signal.pidfd_send_signal', create=True) as send, \
             patch.object(self.guard, 'save', side_effect=OSError('fixture write failed')):
            poll.return_value.poll.return_value = []
            with self.assertRaises(OSError):
                self.guard.enforce_unknown()
            send.assert_not_called()

    def test_unknown_policy_requires_both_explicit_configuration_and_guard_switch(self):
        self.rows.clear()
        with patch.object(self.guard,'enforce_unknown') as intercept:
            self.guard.scan();intercept.assert_not_called()
            self.config['guard']['unknown_npu_processes']=True
            self.guard.next_discovery=0;self.guard.scan();intercept.assert_called_once()
            self.guard.set_enabled({'node':'test','enabled':False,'confirmed':True},ACTOR)
            self.guard.next_discovery=0;self.guard.scan();intercept.assert_called_once()


if __name__ == '__main__': unittest.main()

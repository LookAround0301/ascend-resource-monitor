import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'assets/runtime'))
from container_guard import ContainerGuard, load_candidates
from control_common import ControlError, StopEngine
from process_exporter import guard_metrics, metric_lines
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
        self.assertIn('guard_state="持续拦截"',patched['panels'][-1]['targets'][0]['expr'])
        self.assertEqual(len(patched['panels']), 2)
        self.assertEqual(add_guard_panel(patched)['panels'], patched['panels'])
        self.assertEqual(old['panels'][0]['gridPos']['y'], 4)
        legacy = json.loads(json.dumps(patched))
        legacy['panels'][-1]['title'] = '容器管控 · 持续拦截与白名单'
        legacy['panels'][-1]['targets'][0]['expr'] = 'a5_container_guard_info{node=~"$node"}'
        updated = add_guard_panel(legacy, 'a5')
        self.assertEqual(len(updated['panels']),2)
        self.assertEqual(updated['panels'][-1]['targets'][0]['expr'],'a5_container_guard_info{node=~"$node",guard_state="持续拦截"}')

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


if __name__ == '__main__': unittest.main()

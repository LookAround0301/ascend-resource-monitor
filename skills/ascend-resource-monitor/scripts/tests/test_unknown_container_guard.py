"""Unknown-owner container stops use fresh, instance-scoped evidence."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,os.environ.get('GUARD_TEST_RUNTIME',str(Path(__file__).resolve().parents[2]/'assets/runtime')))
from container_guard import ContainerGuard,load_unknown_candidates
from control_common import ControlError,StopEngine
from process_exporter import guard_metrics

CID='a'*64
START='2026-11-17T00:00:00Z'

class Docker:
    def __init__(self):
        self.info={'Id':CID,'Name':'/fixture','State':{'Running':True,'StartedAt':START},'Config':{'Labels':{}}}
        self.calls=[];self.fail=False;self.removed=False
    def inspect(self,cid):
        if self.removed:raise ControlError(404,'removed')
        return copy.deepcopy(self.info)
    def pids(self,cid):return {23,24}
    def stop(self,cid):
        self.calls.append(cid)
        if self.fail:raise TimeoutError('uncertain fixture')
        self.info['State']['Running']=False

class UnknownContainerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'state.json';self.path.write_text('{"version":1,"entries":{}}')
        self.config={'node':'fixture-node','guard':{'enabled':True,'state_file':str(self.path),
            'unknown_npu_processes':True,'unknown_npu_action':'container'}}
        self.events=[];self.docker=Docker();self.engine=StopEngine(self.config,self.docker,lambda:{23},self.events.append)
        self.guard=ContainerGuard(self.engine,lambda:[],lambda row:True)
        self.rows=[dict(container_id=CID,container='fixture',started_at=START,pid=23,process_key='boot:23:123')]
        self.guard.unknown_candidates=lambda:copy.deepcopy(self.rows)
    def test_scan_stops_whole_container_and_persists_reason_without_permanent_mark(self):
        self.guard.scan();self.assertEqual(self.docker.calls,[CID])
        entry=json.loads(self.path.read_text())['entries'][CID]
        self.assertIsNone(entry['marked_at']);self.assertEqual(entry['unknown_container_interception']['stops'],1)
        self.assertEqual(entry['unknown_container_interception']['result'],'已自动停止')
        self.assertTrue(any(e.get('reason')=='no-employee-evidence' and e.get('result')=='stopped' for e in self.events))
        self.assertIn('未知工号容器拦截','\n'.join(guard_metrics(self.path)))
        self.docker.info['State'].update(Running=True,StartedAt='new-start');self.rows.clear()
        self.guard.next_discovery=0;self.guard.scan();self.assertEqual(self.docker.calls,[CID])
    def test_process_is_revalidated_before_stop(self):
        with patch.object(self.guard,'identity_check',side_effect=[True,False]):self.guard.enforce_unknown()
        self.assertEqual(self.docker.calls,[])
    def test_attribution_is_reloaded_before_stop(self):
        with patch.object(self.guard,'unknown_candidates',side_effect=[copy.deepcopy(self.rows),[]]):self.guard.enforce_unknown()
        self.assertEqual(self.docker.calls,[])
    def test_npu_membership_is_rechecked(self):
        with patch.object(self.engine,'get_npu',side_effect=[{23},set()]):self.guard.enforce_unknown()
        self.assertEqual(self.docker.calls,[])
    def test_restarted_container_is_not_stopped_using_old_snapshot(self):
        first=copy.deepcopy(self.docker.info);last=copy.deepcopy(first);last['State']['StartedAt']='new-start'
        with patch.object(self.docker,'inspect',side_effect=[first,last]):self.guard.enforce_unknown()
        self.assertEqual(self.docker.calls,[])
    def test_protected_and_allowlisted_containers_are_exempt(self):
        for kind in ('protected','whitelisted','personnel_whitelisted','kubernetes'):
            with self.subTest(kind=kind):
                self.config.pop('protected_containers',None);self.guard.entries().clear();self.docker.info['Config']['Labels']={}
                if kind=='protected':self.config['protected_containers']=['fixture']
                elif kind=='kubernetes':self.docker.info['Config']['Labels']={'io.kubernetes.pod.name':'fixture'}
                else:self.guard.entries()[CID]={'whitelisted':kind=='whitelisted','personnel_whitelisted':kind=='personnel_whitelisted'}
                self.guard.enforce_unknown();self.assertEqual(self.docker.calls,[])
    def test_non_npu_and_host_processes_do_not_stop_containers(self):
        self.rows[0]['pid']=24;self.guard.enforce_unknown();self.assertEqual(self.docker.calls,[])
        self.rows[0].update(container_id='',started_at='',pid=23);self.guard.enforce_unknown();self.assertEqual(self.docker.calls,[])
    def test_duplicate_rows_stop_container_once(self):
        self.rows*=2;self.guard.enforce_unknown();self.guard.enforce_unknown()
        self.assertEqual(self.docker.calls,[CID])
    def test_audit_or_state_failure_prevents_stop(self):
        with patch.object(self.engine,'write_audit',side_effect=OSError('audit unavailable')):
            with self.assertRaises(OSError):self.guard.enforce_unknown()
        self.assertEqual(self.docker.calls,[])
        with patch.object(self.guard,'save',side_effect=OSError('state unavailable')):
            with self.assertRaises(OSError):self.guard.enforce_unknown()
        self.assertEqual(self.docker.calls,[])
    def test_stale_inventory_prevents_stop(self):
        with patch.object(self.guard,'unknown_candidates',side_effect=ValueError('stale or incomplete inventory')):
            self.guard.scan()
        self.assertEqual(self.docker.calls,[]);self.assertTrue(self.guard.error)
    def test_uncertain_attempt_survives_restart_without_retry_and_can_be_reconciled(self):
        self.docker.fail=True;self.guard.enforce_unknown();self.assertEqual(self.docker.calls,[CID])
        reloaded=ContainerGuard(self.engine,lambda:[],lambda row:True);reloaded.unknown_candidates=lambda:self.rows
        reloaded.enforce_unknown();self.assertEqual(self.docker.calls,[CID])
        self.rows.clear();self.docker.info['State']['Running']=False;reloaded.enforce_unknown()
        self.assertEqual(reloaded.entries()[CID]['unknown_container_interception']['result'],'已自动停止')
        reloaded.enforce_unknown();self.assertEqual(reloaded.entries()[CID]['unknown_container_interception']['stops'],1)
    def test_removed_container_is_reconciled_after_uncertain_stop(self):
        self.docker.fail=True;self.guard.enforce_unknown()
        self.docker.removed=True;self.rows.clear();self.guard.enforce_unknown()
        self.assertEqual(self.guard.entries()[CID]['unknown_container_interception']['result'],'已自动停止')
        self.assertEqual(self.docker.calls,[CID])
    def test_restarted_instance_does_not_repeat_uncertain_stop_without_new_unknown_evidence(self):
        self.docker.fail=True;self.guard.enforce_unknown()
        self.docker.info['State'].update(Running=True,StartedAt='new-start');self.rows.clear()
        self.guard.enforce_unknown()
        self.assertEqual(self.guard.entries()[CID]['unknown_container_interception']['result'],'已自动停止')
        self.assertEqual(self.docker.calls,[CID])
    def test_incomplete_container_metadata_does_not_confirm_uncertain_stop(self):
        self.docker.fail=True;self.guard.enforce_unknown();self.rows.clear()
        self.docker.info['State']={};self.guard.enforce_unknown()
        self.assertEqual(self.guard.entries()[CID]['unknown_container_interception']['result'],'停止结果待核实')
        self.assertEqual(self.guard.entries()[CID]['unknown_container_interception']['stops'],0)
    def test_new_container_instance_requires_new_unknown_evidence(self):
        self.guard.enforce_unknown();self.docker.info['State'].update(Running=True,StartedAt='new-start')
        self.guard.enforce_unknown();self.assertEqual(self.docker.calls,[CID])
        self.rows[0]['started_at']='new-start';self.guard.enforce_unknown();self.assertEqual(self.docker.calls,[CID,CID])
    def test_disabled_guard_does_not_stop_unknown_container(self):
        self.guard.state['enabled']=False;self.guard.scan();self.assertEqual(self.docker.calls,[])
    def test_container_mode_requires_explicit_configuration(self):
        self.config['guard'].pop('unknown_npu_action')
        with self.assertRaises(ValueError):self.guard.enforce_unknown_containers()
        self.assertEqual(self.docker.calls,[])
    def test_loader_excludes_recognized_and_ambiguous_owners_including_extended_ids(self):
        import time
        def metric(employee,attribution):
            labels=dict(employee_id=employee,attribution=attribution,pid='23',process_key='boot:23:123',
                container='fixture',control_ref=CID+'@'+START)
            return 'ascend_process_start_time_seconds{'+','.join(k+'='+json.dumps(v,ensure_ascii=False) for k,v in labels.items())+'} 1'
        header=('ascend_process_inventory_success 1\nascend_process_inventory_truncated 0\n'
            'ascend_process_container_metadata_age_seconds 1\nascend_process_inventory_timestamp_seconds '+str(time.time())+'\n')
        lines=[metric('未知','无可用工号线索'),metric('a12345678','运行用户'),metric('p_fixture','运行用户'),
            metric('awx1234567','运行用户'),metric('待确认','多个工号线索'),metric('未知','归属待核实')]
        raw=header+'\n'.join(lines)
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return raw.encode()
        with patch('container_guard.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value=Response()
            self.assertEqual(len(load_unknown_candidates({'process_url':'http://127.0.0.1/metrics'})),1)
            for original,replacement in [('inventory_success 1','inventory_success 0'),('inventory_truncated 0','inventory_truncated 1'),
                ('container_metadata_age_seconds 1','container_metadata_age_seconds 100')]:
                raw=header.replace(original,replacement)+'\n'.join(lines)
                with self.assertRaises(ValueError):load_unknown_candidates({'process_url':'http://127.0.0.1/metrics'})

if __name__=='__main__':unittest.main()

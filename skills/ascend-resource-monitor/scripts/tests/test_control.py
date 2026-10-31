from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
import copy
import json
from pathlib import Path
import tempfile
import unittest

from control_common import ControlError, StopEngine, check_browser_request, is_admin, parse_browser_payload, validate_request
from patch_dashboard import add_stop_button
from control_ui import confirmation

CID='a'*64
STAMP='2026-10-31T00:00:00.123456789Z'
ACTOR={'id':1,'login':'admin','orgId':1}


def payload():
    return {'node':'node-a','container_id':CID,'container_started_at':STAMP,'confirmed':True}


class FakeDocker:
    def __init__(self):
        self.info={'Id':CID,'Name':'/test-workload','State':{'Running':True,'StartedAt':STAMP},'Config':{'Labels':{}}}
        self.stops=[]
        self.processes={101,102}
        self.fail_stop=False
    def inspect(self,cid): return copy.deepcopy(self.info)
    def pids(self,cid): return self.processes
    def stop(self,cid):
        self.stops.append(cid)
        if self.fail_stop: raise TimeoutError()
        self.info['State']['Running']=False


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.docker=FakeDocker(); self.events=[]
        self.engine=StopEngine({'node':'node-a'},self.docker,lambda:{101},self.events.append)

    def test_admin_requires_real_identity_and_current_org(self):
        self.assertFalse(is_admin({},[],1))
        viewer={'id':5,'orgId':1,'isGrafanaAdmin':False}
        self.assertFalse(is_admin(viewer,[{'orgId':1,'role':'Viewer'}],1))
        self.assertFalse(is_admin(viewer,[{'orgId':1,'role':'Editor'}],1))
        self.assertTrue(is_admin(viewer,[{'orgId':1,'role':'Admin'}],1))
        self.assertFalse(is_admin({**viewer,'orgId':2},[{'orgId':2,'role':'Admin'}],1))
        self.assertTrue(is_admin({**viewer,'isGrafanaAdmin':True},[],1))
        self.assertFalse(is_admin({**viewer,'isGrafanaAdmin':True,'isDisabled':True},[],1))

    def test_csrf_origin_content_type_and_custom_header(self):
        h={'Origin':'http://localhost:13000','X-A5-Control':'stop-v1','Content-Type':'application/json'}
        check_browser_request(h,'localhost:13000','http')
        for changed in ({'Origin':'https://evil.example'},{'X-A5-Control':''},{'Content-Type':'text/plain'}):
            with self.assertRaises(ControlError):check_browser_request({**h,**changed},'localhost:13000','http')

    def test_target_roundtrip_and_strict_input(self):
        result=parse_browser_payload({'node':'node-a','target':CID+'@'+STAMP,'confirmed':True})
        self.assertEqual(result,payload())
        for target in ['', 'test-workload', '../'+CID+'@'+STAMP, CID+'@'+STAMP+'@extra']:
            with self.assertRaises(ControlError): parse_browser_payload({'node':'node-a','target':target,'confirmed':True})
        for changed in ({'confirmed':False},{'confirmed':1},{'node':'127.0.0.1; rm'},{'cmd':'stop'}):
            with self.assertRaises(ControlError): validate_request({**payload(),**changed})

    def test_stop_exact_container_once_after_audit(self):
        result=self.engine.stop(payload(),ACTOR)
        self.assertTrue(result['stopped'])
        self.assertEqual(self.docker.stops,[CID])
        self.assertEqual([e['result'] for e in self.events],['requested','stopped'])
        with self.assertRaises(ControlError):self.engine.stop(payload(),ACTOR)
        self.assertEqual(self.docker.stops,[CID])

    def test_restarted_or_foreign_container_is_not_stopped(self):
        for changed in ({'node':'node-b'},{'container_started_at':'2026-10-31T01:00:00Z'},{'container_id':'b'*64}):
            with self.assertRaises(ControlError):self.engine.stop({**payload(),**changed},ACTOR)
        self.assertFalse(self.docker.stops)

    def test_non_npu_container_is_not_stopped(self):
        self.docker.processes={999}
        with self.assertRaises(ControlError):self.engine.stop(payload(),ACTOR)
        self.assertFalse(self.docker.stops)

    def test_kubernetes_and_protected_containers_are_not_stopped(self):
        self.docker.info['Config']['Labels']={'io.kubernetes.pod.name':'system-pod'}
        with self.assertRaises(ControlError):self.engine.stop(payload(),ACTOR)
        self.docker.info['Config']['Labels']={}
        self.engine.config['protected_containers']=['test-workload']
        with self.assertRaises(ControlError):self.engine.stop(payload(),ACTOR)
        self.assertFalse(self.docker.stops)

    def test_failed_identity_or_audit_prevents_execution(self):
        with self.assertRaises(ControlError):self.engine.stop(payload(),{})
        def unavailable(event): raise OSError('read only')
        self.engine.write_audit=unavailable
        with self.assertRaises(OSError):self.engine.stop(payload(),ACTOR)
        self.assertFalse(self.docker.stops)

    def test_timeout_is_reported_as_uncertain_not_retried(self):
        self.docker.fail_stop=True
        with self.assertRaises(ControlError) as error:self.engine.stop(payload(),ACTOR)
        self.assertEqual(error.exception.status,504)
        self.assertEqual(self.docker.stops,[CID])
        self.assertEqual(self.events[-1]['result'],'uncertain')

    def test_button_has_confirmation_and_references_exact_container(self):
        d={'uid':'lab','panels':[{'title':'按工号与目录汇总 · 仅 NPU 进程','targets':[{'expr':'sum by(node,employee_id,attribution,container,cwd,host_cwd,directory_group) (metric)'}],'transformations':[{'id':'organize','options':{'renameByName':{}}}],'fieldConfig':{'overrides':[]}}]}
        updated=add_stop_button(d)
        self.assertFalse(updated['editable'])
        override=updated['panels'][0]['fieldConfig']['overrides'][0]
        values={p['id']:p['value'] for p in override['properties']}
        link=values['links'][0]
        self.assertEqual(values['custom.cellOptions']['type'],'data-links')
        self.assertEqual(link['title'],'Stop')
        self.assertTrue(link['targetBlank'])
        self.assertIn('${__value.raw}',link['url'])
        self.assertTrue(link['url'].startswith('/a5-control/confirm?'))
        self.assertIn('control_ref',updated['panels'][0]['targets'][0]['expr'])

    def test_describe_is_read_only_and_checks_instance(self):
        info=self.engine.describe(payload())
        self.assertEqual(info['container'],'test-workload')
        self.assertFalse(self.docker.stops)
        self.assertFalse(self.events)
        with self.assertRaises(ControlError):self.engine.describe({**payload(),'container_started_at':'2026-11-01T00:00:00Z'})

    def test_confirmation_page_escapes_names_and_requires_explicit_submit(self):
        info=self.engine.describe(payload());info['container']='<script>unsafe</script>'
        document=confirmation(ACTOR,payload(),info,'/d/lab')
        self.assertIn('&lt;script&gt;unsafe&lt;/script&gt;',document)
        self.assertNotIn('<script>unsafe',document)
        self.assertIn('id="stop-form"',document)
        self.assertIn('type="submit"',document)
        self.assertIn('admin',document)


if __name__=='__main__':unittest.main()

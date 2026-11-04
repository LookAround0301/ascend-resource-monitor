from pathlib import Path
from datetime import datetime
import sys,time,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
from queue_monitor import QueueMonitor
from patch_dashboard import add_registration_status
from dashboard import dashboard
from test_runtime import example

CID='a'*64
def inventory(now,employee='u12345678',name='Fixture',stamp=None):
    rows=[]
    for suffix,value in [('inventory_success',1),('inventory_truncated',0),('inventory_timestamp_seconds',now if stamp is None else stamp),('container_metadata_age_seconds',0),('inventory_processes',1)]:
        rows.append({'metric':{'__name__':'a5_process_'+suffix,'node':'a'},'value':[now,str(value)]})
    rows.append({'metric':{'__name__':'a5_process_start_time_seconds','node':'a','pid':'123','process_key':'boot:123:456','employee_id':employee,'employee_name':name,'control_ref':CID+'@2026-11-04T00:00:00Z','container':'fixture'},'value':[now,'1']})
    return {'status':'success','data':{'resultType':'vector','result':rows}}

class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.now=time.time();self.monitor=QueueMonitor(['a'],{'metric_prefix':'a5','respect_personnel_whitelist':False},None)
        self.monitor.ingest(inventory(self.now),self.now)
    def state(self,tasks=None):return {'tasks':tasks or [],'server_time':self.now,'alerts':[]}
    def test_unregistered_name_and_employee_broadcast_and_registered_suppression(self):
        result=self.monitor.decorate(self.state());self.assertEqual(len(result['alerts']),1)
        self.assertIn('u12345678',result['alerts'][0]['message']);self.assertIn('Fixture',result['alerts'][0]['message'])
        task={'state':'active','employee':'u12345678','allocations':{'a':[0]}}
        self.assertFalse(self.monitor.decorate(self.state([task]))['alerts'])
        self.assertIn('已登记使用',self.monitor.metrics([task],self.now))
    def test_stale_and_unknown_do_not_accuse(self):
        self.monitor.ingest(inventory(self.now,employee='未知',name=''),self.now)
        self.assertFalse(self.monitor.decorate(self.state())['alerts'])
        self.monitor.ingest(inventory(self.now,stamp=self.now-31),self.now)
        self.assertFalse(self.monitor.decorate(self.state())['alerts'])
        self.assertTrue(self.monitor.decorate(self.state())['process_monitor_errors'])
        self.monitor.ingest(inventory(self.now),self.now)
        self.assertTrue(self.monitor.decorate(self.state())['alerts'])
    def test_continuing_run_exemption_is_bound_to_pre_reset_container_start(self):
        grant={'node':'a','employee':'u12345678','cutoff':datetime.fromisoformat('2026-11-04T01:00:00+00:00').timestamp()}
        self.assertFalse(self.monitor.decorate(self.state(),[grant])['alerts'])
        grant['cutoff']=1
        self.assertTrue(self.monitor.decorate(self.state(),[grant])['alerts'])
    def test_rejects_external_prometheus_endpoint(self):
        with self.assertRaises(ValueError):QueueMonitor(['a'],{'prometheus_url':'http://example.com:9090'},None)
    def test_cached_data_expires_by_inventory_timestamp(self):
        self.monitor.ingest(inventory(self.now,stamp=self.now-25),self.now)
        self.assertFalse(self.monitor.decorate({**self.state(),'server_time':self.now+6})['alerts'])
    def test_process_panel_join_is_idempotent_and_preserves_cpu_series(self):
        source=dashboard(example());result=add_registration_status(source)
        panel=next(p for p in result['panels'] if p['title']=='NPU 进程归属与工作目录')
        self.assertIn('ascend_queue_process_registration',panel['targets'][0]['expr'])
        self.assertIn('or on(node,process_key)',panel['targets'][0]['expr'])
        self.assertEqual(add_registration_status(result),result)
        self.assertEqual(result['panels'][0],source['panels'][0])

if __name__=='__main__':unittest.main()

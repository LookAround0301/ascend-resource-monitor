from pathlib import Path
from datetime import datetime
import sys,time,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
from queue_monitor import QueueMonitor
from patch_dashboard import add_registration_status
from dashboard import dashboard
from test_runtime import example

CID='a'*64
def inventory(now,employee='u12345678',name='Fixture',stamp=None,npu_ids='0'):
    rows=[]
    for suffix,value in [('inventory_success',1),('inventory_truncated',0),('inventory_timestamp_seconds',now if stamp is None else stamp),('container_metadata_age_seconds',0),('inventory_processes',1)]:
        rows.append({'metric':{'__name__':'a5_process_'+suffix,'node':'a'},'value':[now,str(value)]})
    rows.append({'metric':{'__name__':'a5_process_start_time_seconds','node':'a','pid':'123','process_key':'boot:123:456','employee_id':employee,'employee_name':name,'npu_ids':npu_ids,'control_ref':CID+'@2026-11-04T00:00:00Z','container':'fixture'},'value':[now,'1']})
    return {'status':'success','data':{'resultType':'vector','result':rows}}

class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.now=time.time();self.monitor=QueueMonitor(['a'],{'metric_prefix':'a5','respect_personnel_whitelist':False},None)
        self.monitor.ingest(inventory(self.now),self.now)
    def state(self,tasks=None,user=None):return {'tasks':tasks or [],'server_time':self.now,'alerts':[], 'user':user or {'employee':'u12345678'}}
    def test_unregistered_self_notice_and_registered_suppression(self):
        result=self.monitor.decorate(self.state());self.assertEqual(len(result['alerts']),1)
        self.assertIn('你在',result['alerts'][0]['message']);self.assertIn('0 号卡',result['alerts'][0]['message'])
        task={'state':'active','employee':'u12345678','allocations':{'a':[0]}}
        self.assertFalse(self.monitor.decorate(self.state([task]))['alerts'])
        self.assertIn('已登记使用',self.monitor.metrics([task],self.now))
    def test_only_matching_card_owner_and_offender_receive_alerts(self):
        self.monitor.ingest(inventory(self.now,npu_ids='0,2'),self.now)
        owner={'employee':'u23456789'}
        task={'state':'active','employee':'u23456789','allocations':{'a':[2,3]},'mine':True}
        alert=self.monitor.decorate(self.state([task],owner))['alerts']
        self.assertEqual(len(alert),1)
        self.assertIn('2 号卡',alert[0]['message'])
        self.assertNotIn('0、2',alert[0]['message'])
        other={'state':'active','employee':'u23456789','allocations':{'a':[4]},'mine':True}
        self.assertFalse(self.monitor.decorate(self.state([other],owner))['alerts'])
        task['allocations']={'other-node':[2]}
        self.assertFalse(self.monitor.decorate(self.state([task],owner))['alerts'])
        task['allocations']={'a':[2]};task['mine']=False
        self.assertFalse(self.monitor.decorate(self.state([task],owner))['alerts'])
        self.assertTrue(self.monitor.decorate(self.state([task],{'employee':'u12345678'}))['alerts'])
        self.assertFalse(self.monitor.decorate({**self.state([task],owner),'user':None})['alerts'])
        self.monitor.ingest(inventory(self.now,npu_ids='2,unknown'),self.now)
        self.assertFalse(self.monitor.decorate(self.state([task],owner))['alerts'])
    def test_registered_user_excess_notifies_only_affected_active_owner(self):
        self.monitor.ingest(inventory(self.now,npu_ids='0,2,3'),self.now)
        offender={'state':'active','employee':'u12345678','allocations':{'a':[0]},'mine':False}
        affected={'state':'active','employee':'u23456789','allocations':{'a':[2]},'mine':True}
        result=self.monitor.decorate(self.state([offender,affected],{'employee':'u23456789'}))
        alerts=[a for a in result['alerts'] if a['kind']=='over-allocation']
        self.assertEqual(len(alerts),1)
        self.assertIn('2 号卡',alerts[0]['message'])
        self.assertNotIn('3 号卡',alerts[0]['message'])
        affected['mine']=False
        self.assertFalse(self.monitor.decorate(self.state([offender,affected],{'employee':'u34567890'}))['alerts'])
        affected['mine']=True;affected['state']='ready'
        self.assertFalse(self.monitor.decorate(self.state([offender,affected],{'employee':'u23456789'}))['alerts'])
        self.assertFalse(self.monitor.decorate({**self.state([offender,affected]),'user':None})['alerts'])

    def test_extra_card_authorized_by_other_active_or_shared_reservation(self):
        self.monitor.ingest(inventory(self.now,npu_ids='0,2'),self.now)
        offender={'state':'active','employee':'u12345678','allocations':{'a':[0]},'mine':False}
        affected={'state':'active','employee':'u23456789','allocations':{'a':[2]},'mine':True,
                  'allowed_employees':['u12345678']}
        self.assertFalse(self.monitor.decorate(self.state([offender,affected],{'employee':'u23456789'}))['alerts'])
        affected['allowed_employees']=[]
        another={'state':'active','employee':'u12345678','allocations':{'a':[2]},'mine':False}
        self.assertFalse(self.monitor.decorate(self.state([offender,affected,another],{'employee':'u23456789'}))['alerts'])
        self.monitor.ingest(inventory(self.now,npu_ids='0,unknown'),self.now)
        self.assertFalse(self.monitor.decorate(self.state([offender,affected],{'employee':'u23456789'}))['alerts'])
        self.monitor.ingest(inventory(self.now,npu_ids='0,2',stamp=self.now-31),self.now)
        self.assertFalse(self.monitor.decorate(self.state([offender,affected],{'employee':'u23456789'}))['alerts'])

    def test_allowed_user_only_exempt_for_active_allocation_and_matching_cards(self):
        owner={'employee':'u23456789'}
        task={'state':'active','employee':'u23456789','allocations':{'a':[2]},'mine':True,
              'allowed_employees':['u12345678']}
        self.monitor.ingest(inventory(self.now,npu_ids='2'),self.now)
        self.assertFalse(self.monitor.decorate(self.state([task],owner))['alerts'])
        self.assertIn('已登记使用',self.monitor.metrics([task],self.now))
        self.monitor.ingest(inventory(self.now,npu_ids='0,2'),self.now)
        self.assertFalse(self.monitor.decorate(self.state([task],owner))['alerts'])
        self.assertIn('未登记使用',self.monitor.metrics([task],self.now))
        other={'state':'active','employee':'u34567890','allocations':{'a':[0]},'mine':True,'allowed_employees':[]}
        task['mine']=False
        alert=self.monitor.decorate(self.state([task,other],{'employee':'u34567890'}))['alerts']
        self.assertEqual(len(alert),1)
        self.assertIn('0 号卡',alert[0]['message'])
        self.assertNotIn('2 号卡',alert[0]['message'])
        task['state']='ready';other['mine']=False
        self.monitor.ingest(inventory(self.now,npu_ids='2'),self.now)
        self.assertIn('未登记使用',self.monitor.metrics([task,other],self.now))
        self.monitor.ingest(inventory(self.now,npu_ids='unknown'),self.now)
        task['state']='active'
        self.assertIn('待核实',self.monitor.metrics([task,other],self.now))
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
    def test_shared_continuing_is_limited_to_cards_and_original_run(self):
        grant={'node':'a','employee':'u12345678','cards':[2],
               'cutoff':datetime.fromisoformat('2026-11-04T01:00:00+00:00').timestamp()}
        self.monitor.ingest(inventory(self.now,npu_ids='2'),self.now)
        self.assertIn('跨日继续使用',self.monitor.metrics([],self.now,[grant]))
        self.monitor.ingest(inventory(self.now,npu_ids='0,2'),self.now)
        self.assertIn('未登记使用',self.monitor.metrics([],self.now,[grant]))
        alert=self.monitor.decorate(self.state(),[grant])['alerts']
        self.assertEqual(len(alert),1)
        self.assertIn('0 号卡',alert[0]['message'])
        grant['cutoff']=1
        self.monitor.ingest(inventory(self.now,npu_ids='2'),self.now)
        self.assertIn('未登记使用',self.monitor.metrics([],self.now,[grant]))
    def test_rejects_external_prometheus_endpoint(self):
        with self.assertRaises(ValueError):QueueMonitor(['a'],{'prometheus_url':'http://example.com:9090'},None)
    def test_cached_data_expires_by_inventory_timestamp(self):
        self.monitor.ingest(inventory(self.now,stamp=self.now-25),self.now)
        self.assertFalse(self.monitor.decorate({**self.state(),'server_time':self.now+6})['alerts'])
    def test_small_future_clock_skew_keeps_registered_process_status(self):
        task={'state':'active','employee':'u12345678','allocations':{'a':[0]}}
        for ahead in (1,10,15):
            with self.subTest(ahead=ahead):
                self.monitor.ingest(inventory(self.now,stamp=self.now+ahead),self.now)
                self.assertFalse(self.monitor.decorate(self.state([task]))['process_monitor_errors'])
                self.assertIn('已登记使用',self.monitor.metrics([task],self.now))
                self.assertFalse(self.monitor.current(self.now+21))
        self.monitor.ingest(inventory(self.now,stamp=self.now+16),self.now)
        self.assertTrue(self.monitor.decorate(self.state([task]))['process_monitor_errors'])
        self.assertNotIn('已登记使用',self.monitor.metrics([task],self.now))
    def test_process_panel_join_is_idempotent_and_preserves_cpu_series(self):
        source=dashboard(example());result=add_registration_status(source)
        panel=next(p for p in result['panels'] if p['title']=='NPU 进程归属与工作目录')
        self.assertIn('ascend_queue_process_registration',panel['targets'][0]['expr'])
        self.assertIn('or on(node,process_key)',panel['targets'][0]['expr'])
        self.assertEqual(add_registration_status(result),result)
        self.assertEqual(result['panels'][0],source['panels'][0])

if __name__=='__main__':unittest.main()

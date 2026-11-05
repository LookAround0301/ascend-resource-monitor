from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest
import asyncio
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'assets/runtime'))
from queue_monitor import QueueMonitor
from queue_store import QueueStore
from queue_unified import UnifiedQueue
try:
    from aiohttp import web
    from aiohttp.test_utils import TestClient,TestServer
except ImportError:
    web=None
from queue_http import install_queue, COOKIE


def stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


def inventory(now, processes, invalid_node=None):
    result=[]
    for node in ('a','b'):
        node_rows=[r for r in processes if r['node']==node]
        for name,value in [('inventory_success',0 if node==invalid_node else 1),('inventory_truncated',0),
                           ('inventory_timestamp_seconds',now),('container_metadata_age_seconds',0),('inventory_processes',len(node_rows))]:
            result.append({'metric':{'__name__':'a5_process_'+name,'node':node},'value':[now,str(value)]})
        for index,row in enumerate(node_rows):
            labels={'__name__':'a5_process_start_time_seconds','node':node,'pid':str(index+1),
                    'process_key':node+':'+str(index+1),'employee_id':row.get('employee','u12345678'),
                    'employee_name':'Fixture','npu_ids':row.get('cards','0'),'control_ref':'','container':''}
            result.append({'metric':labels,'value':[now,'1']})
    return {'status':'success','data':{'resultType':'vector','result':result}}


class NightAutoFinishTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=QueueStore(Path(self.temp.name)/'queue.db')
        self.user=self.store.user(self.store.register('u12345678','Fixture','fixture-password'))
        self.start=stamp('2026-11-04T20:00:00')
        self.clock=patch('queue_unified.time.time',return_value=self.start-60).start();self.addCleanup(patch.stopall)
        self.queue=UnifiedQueue(self.store,['a','b'],timezone='Asia/Shanghai')
        self.monitor=QueueMonitor(['a','b'],{'metric_prefix':'a5'},None)

    def add(self, machine='a', cards=8):
        return self.queue.enqueue(self.user,{'machine':machine,'cards':cards,'requested_seconds':3600,
                                            'night':True,'not_before':self.start,'notifications_ready':True})

    def sample(self, now, processes=(), invalid_node=None):
        self.clock.return_value=now
        self.monitor.ingest(inventory(now,processes,invalid_node),now)
        return self.queue.auto_finish_nights(self.monitor,now)

    def rows(self):
        with self.store.db() as db:
            return {r['id']:dict(r) for r in db.execute('SELECT * FROM unified_tasks')}

    def test_observed_usage_then_three_empty_samples_finishes_and_advances_queue(self):
        first=self.add();second=self.add()
        self.clock.return_value=self.start;self.queue.tick()
        self.sample(self.start,[{'node':'a','cards':'0,1'}])
        self.assertEqual(self.rows()[first]['night_seen_usage'],1)
        for elapsed in (1,11):
            self.assertFalse(self.sample(self.start+elapsed))
            self.assertIn(first,self.rows())
        self.assertEqual(self.sample(self.start+21),{first})
        rows=self.rows();self.assertNotIn(first,rows);self.assertEqual(rows[second]['state'],'active')

    def test_never_observed_waits_until_planned_end_even_if_cards_empty(self):
        job=self.add();self.clock.return_value=self.start;self.queue.tick()
        for elapsed in (1,11,21,100):self.assertFalse(self.sample(self.start+elapsed))
        self.assertIn(job,self.rows())
        for elapsed in (3600,3610):self.assertFalse(self.sample(self.start+elapsed))
        self.assertEqual(self.sample(self.start+3620),{job})

    def test_repeated_cached_sample_does_not_count_as_new_evidence(self):
        job=self.add();self.clock.return_value=self.start;self.queue.tick()
        self.sample(self.start,[{'node':'a','cards':'0'}])
        self.sample(self.start+1)
        for elapsed in (11,20,21):
            self.assertFalse(self.queue.auto_finish_nights(self.monitor,self.start+elapsed))
        self.assertIn(job,self.rows())

    def test_observed_allowed_users_count_and_survive_monitor_restart(self):
        job=self.add();self.queue.allowed_user(self.user,job,'u23456789','add')
        self.clock.return_value=self.start;self.queue.tick()
        self.sample(self.start,[{'node':'a','cards':'2','employee':'u23456789'}])
        self.assertEqual(self.rows()[job]['night_seen_usage'],1)
        self.monitor=QueueMonitor(['a','b'],{'metric_prefix':'a5'},None)
        for elapsed in (1,11):self.assertFalse(self.sample(self.start+elapsed))
        self.assertEqual(self.sample(self.start+21),{job})

    def test_other_process_invalid_cards_and_stale_inventory_prevent_release(self):
        job=self.add();self.clock.return_value=self.start;self.queue.tick()
        self.sample(self.start,[{'node':'a','cards':'0'}])
        self.sample(self.start+1)
        self.sample(self.start+11,invalid_node='a')
        self.sample(self.start+21)
        self.sample(self.start+31,[{'node':'a','cards':'0','employee':'u99999999'}])
        self.sample(self.start+41,[{'node':'a','cards':'unknown'}])
        self.assertIn(job,self.rows())
        for elapsed in (51,61):self.assertFalse(self.sample(self.start+elapsed))
        self.assertEqual(self.sample(self.start+71),{job})

    def test_dual_requires_three_fresh_empty_samples_on_both_nodes(self):
        job=self.add('dual',16);self.clock.return_value=self.start;self.queue.tick()
        self.sample(self.start,[{'node':'a','cards':'0'}])
        self.sample(self.start+1)
        self.sample(self.start+11,invalid_node='b')
        self.sample(self.start+21)
        self.sample(self.start+31)
        self.assertIn(job,self.rows())
        self.assertEqual(self.sample(self.start+41),{job})

    def test_normal_active_reservation_is_never_automatically_removed(self):
        job=self.queue.enqueue(self.user,{'machine':'a','cards':8,'requested_seconds':60,'notifications_ready':True})
        offer=self.queue.view(self.user)['tasks'][0]['offer_id'];self.queue.action(self.user,job,'claim',offer)
        for elapsed in (120,130,140,150):self.sample(self.start+elapsed)
        self.assertIn(job,self.rows())

    def test_night_running_after_nine_requires_manual_finish_and_keeps_times(self):
        start=stamp('2026-11-05T08:00:00');self.start=start;self.clock.return_value=start-60
        job=self.add();self.clock.return_value=start;self.queue.tick()
        before=self.rows()[job]
        for elapsed in (3600,3610,3620,3630):self.assertFalse(self.sample(start+elapsed))
        after=self.rows()[job]
        self.assertEqual((after['activated'],after['requested_seconds']),(before['activated'],before['requested_seconds']))
        self.assertTrue(self.queue.view(self.user)['tasks'][0]['morning_carryover'])
        self.queue.action(self.user,job,'finish')
        self.assertNotIn(job,self.rows())

    def test_scheduled_night_handoff_ignores_running_npu_process(self):
        first=self.add();second=self.add();self.clock.return_value=self.start;self.queue.tick()
        self.sample(self.start,[{'node':'a','cards':'0'}])
        self.clock.return_value=self.start+3600
        self.monitor.ingest(inventory(self.start+3600,[{'node':'a','cards':'0'}]),self.start+3600)
        self.queue.tick()
        rows=self.rows()
        self.assertNotIn(first,rows)
        self.assertEqual((rows[second]['state'],rows[second]['activated']),('active',self.start+3600))


@unittest.skipUnless(web, 'aiohttp is required for background integration')
class NightAutoFinishHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.start=stamp('2026-11-04T20:00:00')
        self.clock=patch('queue_unified.time.time',return_value=self.start-60).start();self.addCleanup(patch.stopall)
        self.processes=[]
        async def poll(monitor):
            now=self.clock.return_value
            monitor.ingest(inventory(now,self.processes),now)
        self.poll_patch=patch.object(QueueMonitor,'poll',poll);self.poll_patch.start();self.addCleanup(self.poll_patch.stop)
        async def identity(request):return {'admin':False}
        async def admin(request):raise AssertionError('unexpected admin request')
        cfg={'nodes':{'a':{},'b':{}},'audit_log':str(Path(self.temp.name)/'audit.jsonl'),
             'task_queue':{'database':str(Path(self.temp.name)/'queue.db'),'unified_queue':True,
                           'monitor_processes':True,'metric_prefix':'a5','timezone':'Asia/Shanghai'}}
        app=web.Application();self.store=install_queue(app,cfg,identity,admin)
        self.client=TestClient(TestServer(app));await self.client.start_server()
        self.token=self.store.register('u12345678','Fixture','fixture-password')

    async def asyncTearDown(self):
        await self.client.close();self.temp.cleanup()

    async def test_background_loop_finishes_night_without_browser_polling(self):
        headers={'Origin':str(self.client.make_url('')).rstrip('/'),'X-A5-Queue':'queue-v1','Cookie':COOKIE+'='+self.token}
        response=await self.client.post('/a5-queue/api/enqueue',headers=headers,json={
            'machine':'a','cards':8,'requested_seconds':3600,'night':True,'not_before':self.start,'notifications_ready':True})
        self.assertEqual(response.status,200);job=(await response.json())['job']
        self.processes=[{'node':'a','cards':'0'}];self.clock.return_value=self.start
        await asyncio.sleep(3.3)
        with self.store.db() as db:
            row=db.execute('SELECT state,night_seen_usage FROM unified_tasks WHERE id=?',(job,)).fetchone()
        self.assertEqual((row['state'],row['night_seen_usage']),('active',1))
        self.processes=[]
        for elapsed in (1,11,21):
            self.clock.return_value=self.start+elapsed
            await asyncio.sleep(3.3)
        with self.store.db() as db:
            self.assertIsNone(db.execute('SELECT id FROM unified_tasks WHERE id=?',(job,)).fetchone())


if __name__=='__main__':unittest.main()

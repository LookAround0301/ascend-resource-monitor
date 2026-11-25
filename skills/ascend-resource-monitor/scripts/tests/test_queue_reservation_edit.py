"""A5 edits preserve identity, queue order, claim deadlines and reservation policy."""
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
from control_common import ControlError
from queue_store import QueueStore
from queue_unified import UnifiedQueue


class ReservationEditTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.store=QueueStore(Path(folder.name)/'queue.db')
        self.one=self.store.user(self.store.register('u12345678','One','fixture-password'))
        self.two=self.store.user(self.store.register('u12345679','Two','fixture-password'))
        self.queue=self.reopen()
        self.now=datetime(2026,11,25,10,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        clock=patch('queue_unified.time.time',side_effect=lambda:self.now);clock.start();self.addCleanup(clock.stop)

    def reopen(self):
        return UnifiedQueue(self.store,['a','b'],timezone='Asia/Shanghai',strict_nights=True,
                            midnight_separation=True,overtime_control=True,normal_max_seconds=3600,normal_reservation_limit=3,
                            night_carryover_cutoff_hour=9)

    def add(self,user=None,**data):
        return self.queue.enqueue(user or self.one,{'machine':'a','cards':1,'requested_seconds':3600,
                                 'notifications_ready':True,'purpose':'模型验证',**data})

    def row(self,job):
        with self.store.db() as db:return dict(db.execute('SELECT * FROM unified_tasks WHERE id=?',(job,)).fetchone())

    def edit(self,job,user=None,**data):
        row=self.row(job)
        return self.queue.update_reservation(user or self.one,job,{'offer_id':row['offer_id'],**data})

    def rejected(self,status,call):
        with self.assertRaises(ControlError) as caught:call()
        self.assertEqual(caught.exception.status,status)

    def night(self,hour):
        return datetime(2026,11,26,hour,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()

    def test_purpose_roundtrip_and_legacy_create(self):
        job=self.add(purpose='  模型验证 <script>  ')
        self.queue=self.reopen()
        self.assertEqual(next(r['purpose'] for r in self.queue.view(self.one)['tasks'] if r['id']==job),'模型验证 <script>')
        legacy=self.queue.enqueue(self.two,{'cards':1,'requested_seconds':60,'notifications_ready':True})
        self.assertEqual(self.row(legacy)['purpose'],'')
        self.edit(legacy,user=self.two,purpose='补充事项')
        self.assertEqual(self.row(legacy)['purpose'],'补充事项')

    def test_invalid_purpose_does_not_change_reservation(self):
        job=self.add();before=self.row(job)
        for value in ('','   ','x'*121,'a\nb','a\x00b','a\u2028b',None,123):
            self.rejected(400,lambda:self.edit(job,purpose=value))
        self.assertEqual(self.row(job),before)

    def test_purpose_only_preserves_offer_and_allocation(self):
        job=self.add();before=self.row(job)
        self.edit(job,purpose='性能测试')
        after=self.row(job);after['purpose']=before['purpose']
        self.assertEqual(before,after)

    def test_ready_resource_edit_preserves_order_and_claim_deadline(self):
        job=self.add();before=self.row(job);self.now+=120
        self.edit(job,machine='b',cards=2,requested_seconds=1800,purpose='调整测试')
        after=self.row(job)
        for field in ('id','account','created','queue_order','allowed_employees','avoid_slots','missed_claims'):
            self.assertEqual(after[field],before[field],field)
        self.assertEqual(after['state'],'ready');self.assertEqual(after['cards'],2)
        self.assertEqual(list(json.loads(after['allocations'])),['b'])
        self.assertEqual(after['requested_seconds'],1800)
        self.assertLessEqual(after['claim_before'],before['claim_before'])
        self.assertEqual(after['ready_since'],before['ready_since'])
        self.assertNotEqual(after['offer_id'],before['offer_id'])
        self.rejected(409,lambda:self.queue.action(self.one,job,'claim',before['offer_id']))

    def test_three_booking_limit_does_not_block_edit(self):
        jobs=[self.add() for _ in range(3)]
        self.rejected(409,lambda:self.add())
        self.edit(jobs[-1],machine='dual',cards=4,requested_seconds=1200)
        row=self.row(jobs[-1]);self.assertEqual((row['cards'],row['dual']),(2,1))
        self.assertEqual(len(self.queue.view(self.one)['tasks']),3)

    def test_permissions_and_active_or_closed_state(self):
        job=self.add()
        self.rejected(403,lambda:self.edit(job,user=self.two,purpose='forbidden'))
        with self.store.db() as db:db.execute('UPDATE unified_tasks SET allowed_employees=? WHERE id=?',(json.dumps([self.two['employee']]),job))
        self.rejected(403,lambda:self.edit(job,user=self.two,purpose='shared'))
        self.edit(job,user={**self.two,'admin':True},purpose='管理员修改')
        self.queue.action(self.one,job,'claim',self.row(job)['offer_id'])
        self.rejected(409,lambda:self.edit(job,purpose='active'))
        self.queue.action(self.one,job,'finish')
        self.rejected(404,lambda:self.queue.update_reservation(self.one,job,{'purpose':'closed'}))

    def test_stale_offer_and_invalid_resources_or_duration(self):
        job=self.add();before=self.row(job)
        self.rejected(409,lambda:self.edit(job,offer_id='stale',machine='b',cards=2))
        for data in ({'machine':'c','cards':1},{'machine':'dual','cards':3},{'cards':2},
                     {'machine':'a','cards':True},{'requested_seconds':3601},{'requested_seconds':3660},
                     {'requested_seconds':True},{'requested_seconds':0}):
            self.rejected(400,lambda:self.edit(job,**data))
        self.assertEqual(self.row(job),before)

    def test_ready_edit_can_return_to_queue_without_losing_place(self):
        first=self.add(cards=8);second=self.add(self.two,machine='b',cards=8)
        before=self.row(second);first_before=self.row(first)
        self.edit(second,user=self.two,machine='a',cards=8)
        row=self.row(second)
        self.assertEqual(row['state'],'queued');self.assertEqual(row['offer_id'],'')
        self.assertEqual(row['queue_order'],before['queue_order'])
        self.assertEqual(self.row(first)['allocations'],first_before['allocations'])

    def test_shortening_disables_incompatible_backfill_only(self):
        job=self.add(backfill_min_seconds=1800,avoid_slots=[30])
        self.queue.set_default_avoid_slots(self.one,[32])
        self.assertTrue(self.edit(job,requested_seconds=600))
        self.assertEqual(self.row(job)['backfill_min_seconds'],0)
        self.assertEqual(json.loads(self.row(job)['avoid_slots']),[30])
        self.assertEqual(self.queue.default_avoid_slots(self.one),[32])

    def test_night_conflict_is_atomic_and_duration_only_keeps_cards(self):
        first=self.add(night=True,not_before=self.night(1),cards=8)
        later=self.add(self.two,night=True,not_before=self.night(2),cards=8)
        before=self.row(first)
        self.rejected(409,lambda:self.edit(first,requested_seconds=7200,purpose='冲突'))
        self.assertEqual(self.row(first),before)
        self.edit(first,requested_seconds=1800)
        self.assertEqual(self.row(first)['allocations'],before['allocations'])
        self.edit(first,machine='b',cards=4,requested_seconds=7200)
        self.assertEqual(list(json.loads(self.row(first)['allocations'])),['b'])
        self.assertEqual(self.row(later)['fixed_node'],'a')

    def test_night_cutoff_and_takeover_guard(self):
        job=self.add(night=True,not_before=self.night(8))
        self.edit(job,requested_seconds=7200)
        self.assertEqual(self.row(job)['requested_seconds'],3600)
        with self.store.db() as db:db.execute('UPDATE unified_tasks SET night_takeover=1 WHERE id=?',(job,))
        self.rejected(409,lambda:self.edit(job,purpose='接管中'))

    def test_edit_schedule_failure_rolls_back(self):
        job=self.add();before=self.row(job);original=self.queue._schedule;count=0
        def schedule(*args,**kwargs):
            nonlocal count
            count+=1
            if count==2:raise RuntimeError('failed scheduling')
            return original(*args,**kwargs)
        with patch.object(self.queue,'_schedule',side_effect=schedule):
            with self.assertRaises(RuntimeError):self.edit(job,machine='b',cards=2,purpose='rollback')
        self.assertEqual(self.row(job),before)


class ReservationEditHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient,TestServer
        import queue_http
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        monitor=patch('queue_monitor.QueueMonitor.poll');monitor.start();self.addCleanup(monitor.stop)
        async def identity(request):return {'admin':False,'user':None}
        async def admin(request):raise AssertionError('Unexpected admin endpoint')
        app=web.Application()
        config={'nodes':{'a':{},'b':{}},'audit_log':str(Path(folder.name)/'audit.jsonl'),
                'task_queue':{'database':str(Path(folder.name)/'queue.db'),'unified_queue':True,
                              'strict_nights':True,'monitor_processes':True,'timezone':'Asia/Shanghai'}}
        self.store=queue_http.install_queue(app,config,identity,admin)
        self.client=TestClient(TestServer(app));await self.client.start_server()
        self.cookie=queue_http.session_cookie_name(self.store.session_namespace,self.client.make_url('').authority) if hasattr(queue_http,'session_cookie_name') else queue_http.COOKIE
        self.one=self.store.register('u12345678','One','fixture-password')
        self.two=self.store.register('u12345679','Two','fixture-password')

    async def asyncTearDown(self):await self.client.close()

    def headers(self,token):
        return {'Origin':str(self.client.make_url('')).rstrip('/'),'X-A5-Queue':'queue-v1','Cookie':self.cookie+'='+token}

    async def post(self,op,data,token,status=200):
        response=await self.client.post('/a5-queue/api/'+op,json=data,headers=self.headers(token))
        result=await response.json();self.assertEqual(response.status,status,result);return result

    async def test_http_edit_purpose_and_guards(self):
        job=(await self.post('enqueue',{'cards':1,'requested_seconds':3600,'notifications_ready':True,'purpose':'接口验证'},self.one))['job']
        state=await (await self.client.get('/a5-queue/api/state',headers=self.headers(self.one))).json()
        row=next(r for r in state['tasks'] if r['id']==job)
        data={'job':job,'offer_id':row['offer_id'],'purpose':'修改事项','confirmed':True}
        await self.post('unified-resources',data,'invalid',401)
        await self.post('unified-resources',data,self.two,403)
        await self.post('unified-resources',{**data,'confirmed':False},self.one,400)
        await self.post('unified-resources',data,self.one)
        state=await (await self.client.get('/a5-queue/api/state',headers=self.headers(self.one))).json()
        self.assertEqual(next(r['purpose'] for r in state['tasks'] if r['id']==job),'修改事项')


if __name__=='__main__':unittest.main()

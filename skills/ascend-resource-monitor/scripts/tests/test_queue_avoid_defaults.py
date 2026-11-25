"""Account isolation, persistence and atomic updates for default avoid slots."""
from datetime import datetime
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

MEALS=[24,25,26,27,35,36,37,38]


class AvoidDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'queue.db';self.store=QueueStore(self.path)
        self.one=self.store.user(self.store.register('u12345678','One','fixture-password'))
        self.two=self.store.user(self.store.register('u12345679','Two','fixture-password'))
        self.queue=self.reopen()
        clock=patch('queue_unified.time.time',return_value=datetime(2026,11,23,10,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp())
        clock.start();self.addCleanup(clock.stop)

    def reopen(self):
        return UnifiedQueue(QueueStore(self.path),['a','b'],timezone='Asia/Shanghai',strict_nights=True)

    def add(self,user=None,**data):
        return self.queue.enqueue(user or self.one,{'purpose':'测试预约','cards':1,'requested_seconds':3600,'notifications_ready':True,**data})

    def slots(self,job):
        return next(row['avoid_slots'] for row in self.queue.view(self.one)['tasks'] if row['id']==job)

    def test_defaults_persist_and_are_isolated_between_accounts(self):
        job=self.add();self.queue.update_avoid_slots(self.one,job,[20,21,20],True)
        self.queue=self.reopen()
        self.assertEqual(self.queue.view(self.one)['default_avoid_slots'],[20,21])
        self.assertEqual(self.queue.view(self.two)['default_avoid_slots'],MEALS)
        self.assertEqual(self.queue.view(None)['default_avoid_slots'],MEALS)
        self.assertEqual(self.slots(self.add()),[20,21])
        self.assertEqual(self.slots(self.add(self.two)),MEALS)

    def test_empty_default_and_explicit_override_survive_reload(self):
        job=self.add();self.queue.update_avoid_slots(self.one,job,[],True)
        self.queue=self.reopen()
        self.assertEqual(self.queue.default_avoid_slots(self.one),[])
        self.assertEqual(self.slots(self.add()),[])
        self.assertEqual(self.slots(self.add(avoid_slots=[30])),[30])
        self.assertEqual(self.queue.default_avoid_slots(self.one),[])

    def test_unchecked_save_changes_only_selected_reservation(self):
        job=self.add();other=self.add()
        self.queue.update_avoid_slots(self.one,job,[20],True)
        self.queue.update_avoid_slots(self.one,job,[22])
        self.assertEqual(self.slots(job),[22])
        self.assertEqual(self.slots(other),MEALS)
        self.assertEqual(self.queue.default_avoid_slots(self.one),[20])

    def test_rejected_edit_does_not_update_any_defaults(self):
        job=self.add()
        for person in (self.two,{**self.two,'admin':True}):
            with self.assertRaises(ControlError):self.queue.update_avoid_slots(person,job,[],True)
        for flag in ('true',1,None):
            with self.assertRaises(ControlError):self.queue.update_avoid_slots(self.one,job,[],flag)
        with self.assertRaises(ControlError):self.queue.update_avoid_slots(self.one,job,[48],True)
        self.assertEqual(self.slots(job),MEALS)
        self.assertEqual(self.queue.default_avoid_slots(self.one),MEALS)
        self.assertEqual(self.queue.default_avoid_slots(self.two),MEALS)

    def test_schedule_failure_rolls_back_reservation_and_preference(self):
        job=self.add()
        with patch.object(self.queue,'_schedule',side_effect=RuntimeError('fixture failure')):
            with self.assertRaises(RuntimeError):self.queue.update_avoid_slots(self.one,job,[],True)
        self.assertEqual(self.slots(job),MEALS)
        self.assertEqual(self.queue.default_avoid_slots(self.one),MEALS)

    def test_active_and_night_reservations_cannot_set_defaults(self):
        job=self.add()
        row=next(r for r in self.queue.view(self.one)['tasks'] if r['id']==job)
        self.queue.action(self.one,job,'claim',row['offer_id'])
        with self.assertRaises(ControlError):self.queue.update_avoid_slots(self.one,job,[],True)
        night=self.add(night=True,not_before=datetime(2026,11,24,2,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp())
        with self.assertRaises(ControlError):self.queue.update_avoid_slots(self.one,night,[],True)
        self.assertEqual(self.queue.default_avoid_slots(self.one),MEALS)


class AvoidDefaultsHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient,TestServer
        import queue_http
        from queue_http import install_queue
        self.temp=tempfile.TemporaryDirectory()
        monitor=patch('queue_monitor.QueueMonitor.poll')
        monitor.start();self.addCleanup(monitor.stop)
        async def identity(request):return {'admin':False,'user':None}
        async def admin(request):raise AssertionError('No admin operations expected')
        config={'nodes':{'a':{},'b':{}},'audit_log':str(Path(self.temp.name)/'audit.jsonl'),
                'task_queue':{'database':str(Path(self.temp.name)/'queue.db'),'unified_queue':True,'strict_nights':True,'monitor_processes':True,'timezone':'Asia/Shanghai'}}
        app=web.Application();self.store=install_queue(app,config,identity,admin)
        self.client=TestClient(TestServer(app));await self.client.start_server()
        self.cookie=queue_http.session_cookie_name(self.store.session_namespace,self.client.make_url('').authority) if hasattr(queue_http,'session_cookie_name') else queue_http.COOKIE
        self.one=self.store.register('u12345678','One','fixture-password')
        self.two=self.store.register('u12345679','Two','fixture-password')

    async def asyncTearDown(self):
        await self.client.close();self.temp.cleanup()

    def headers(self,token):
        return {'Origin':str(self.client.make_url('')).rstrip('/'),'X-A5-Queue':'queue-v1','Cookie':self.cookie+'='+token}

    async def post(self,operation,data,token,status=200):
        response=await self.client.post('/a5-queue/api/'+operation,json=data,headers=self.headers(token))
        body=await response.json();self.assertEqual(response.status,status,body);return body

    async def state(self,token):
        response=await self.client.get('/a5-queue/api/state',headers=self.headers(token))
        self.assertEqual(response.status,200);return await response.json()

    async def test_save_roundtrip_and_new_reservation_default(self):
        payload={'purpose':'测试预约','cards':1,'requested_seconds':60,'notifications_ready':True}
        job=(await self.post('enqueue',payload,self.one))['job']
        result=await self.post('unified-avoid',{'job':job,'avoid_slots':[],'save_as_default':True,'confirmed':True},self.one)
        self.assertIn('已保存为我的默认',result['message'])
        self.assertEqual((await self.state(self.one))['default_avoid_slots'],[])
        self.assertEqual((await self.state(self.two))['default_avoid_slots'],MEALS)
        next_job=(await self.post('enqueue',payload,self.one))['job']
        self.assertEqual(next(r['avoid_slots'] for r in (await self.state(self.one))['tasks'] if r['id']==next_job),[])
        await self.post('unified-avoid',{'job':job,'avoid_slots':[20],'confirmed':True},self.one)
        self.assertEqual((await self.state(self.one))['default_avoid_slots'],[])

    async def test_other_account_and_invalid_flag_are_rejected(self):
        job=(await self.post('enqueue',{'purpose':'测试预约','cards':1,'requested_seconds':60,'notifications_ready':True},self.one))['job']
        payload={'job':job,'avoid_slots':[],'save_as_default':True,'confirmed':True}
        await self.post('unified-avoid',payload,self.two,403)
        await self.post('unified-avoid',{**payload,'save_as_default':'true'},self.one,400)
        self.assertEqual((await self.state(self.one))['default_avoid_slots'],MEALS)

    async def test_form_saves_default_without_creating_or_editing_reservations(self):
        self.assertEqual((await self.state(self.one))['tasks'],[])
        result=await self.post('unified-avoid-default',{'avoid_slots':[22,20,20],'confirmed':True,'account':self.store.user(self.two)['id']},self.one)
        self.assertEqual(result['default_avoid_slots'],[20,22])
        self.assertEqual((await self.state(self.one))['default_avoid_slots'],[20,22])
        self.assertEqual((await self.state(self.two))['default_avoid_slots'],MEALS)
        self.assertEqual((await self.state(self.one))['tasks'],[])
        job=(await self.post('enqueue',{'purpose':'测试预约','cards':1,'requested_seconds':60,'notifications_ready':True},self.one))['job']
        await self.post('unified-avoid-default',{'avoid_slots':[],'confirmed':True},self.one)
        state=await self.state(self.one)
        self.assertEqual(state['default_avoid_slots'],[])
        self.assertEqual(next(r['avoid_slots'] for r in state['tasks'] if r['id']==job),[20,22])

    async def test_form_default_rejects_unauthorized_unconfirmed_and_invalid_requests(self):
        payload={'avoid_slots':[],'confirmed':True}
        await self.post('unified-avoid-default',payload,'invalid-session',401)
        await self.post('unified-avoid-default',{**payload,'confirmed':False},self.one,400)
        await self.post('unified-avoid-default',{**payload,'avoid_slots':[48]},self.one,400)
        self.assertEqual((await self.state(self.one))['default_avoid_slots'],MEALS)


if __name__=='__main__':unittest.main()

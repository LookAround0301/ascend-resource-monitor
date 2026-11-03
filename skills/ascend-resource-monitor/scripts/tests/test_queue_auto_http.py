"""Auto-match authorization, multiple targets and cancellation reconciliation."""
import copy,json,unittest
import test_queue_http as fixture
from queue_auto import AutoQueue
CID=fixture.CID

class AutoHTTPTests(unittest.IsolatedAsyncioTestCase):
    auto_match=True
    asyncSetUp=fixture.QueueHTTPTests.asyncSetUp
    asyncTearDown=fixture.QueueHTTPTests.asyncTearDown
    headers=fixture.QueueHTTPTests.headers
    post=fixture.QueueHTTPTests.post
    register=fixture.QueueHTTPTests.register
    act=fixture.QueueHTTPTests.act

    async def active_job(self):
        job=(await self.post('enqueue',{'node':'node-a','title':'auto fixture'},self.one))['job']
        await self.act(job,'claim',self.one)
        return job

    async def test_no_manual_binding_and_cancel_uses_server_employee(self):
        job=await self.active_job()
        await self.post('preview',{'job':job,'action':'bind','container_id':CID,'ownership_confirmed':True},self.one,expected=400)
        await self.post('preview',{'job':job,'action':'cancel','employee':'u12345678'},self.two,expected=403)
        await self.act(job,'cancel',self.one)
        calls=[c for c in self.calls if c[1]=='/queue/kill'];self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][2]['request']['snapshot']['owner_employee'],'u12345678')

    async def test_conflicts_source_failure_and_new_target_require_fresh_confirmation(self):
        job=await self.active_job();data={'job':job,'action':'cancel'}
        self.blocked=1;await self.post('preview',data,self.one,expected=409)
        self.blocked=0;self.source_failed=True;await self.post('preview',data,self.one,expected=503)
        self.source_failed=False;preview=await self.post('preview',data,self.one)
        extra=copy.deepcopy(self.owned_targets[0]);extra['container_id']='b'*64;self.owned_targets.append(extra)
        await self.post('confirm',{**data,'token':preview['token'],'confirmed':True},self.one,expected=409)
        self.assertFalse(any(c[1]=='/queue/kill' for c in self.calls))

    async def test_multi_target_cancel_waits_for_every_result_and_remaining_work(self):
        job=await self.active_job();extra=copy.deepcopy(self.owned_targets[0]);extra['container_id']='b'*64;self.owned_targets.append(extra)
        await self.act(job,'cancel',self.one)
        row=self.store.cancelling()[0];self.assertEqual(len(json.loads(row['cancel_batch'])),2)
        self.assertEqual(len([c for c in self.calls if c[1]=='/queue/kill']),2)
        async def remaining(node,path,data=None):
            return {'targets':self.owned_targets,'blocked':0} if '/owned?' in path else {'state':'succeeded','message':'done'}
        await AutoQueue(self.store,remaining).reconcile(row)
        self.assertEqual(self.store.own(self.store.user(self.one),job)['state'],'active')
        await self.act(job,'cancel',self.one)
        self.owned_targets=[];await AutoQueue(self.store,remaining).reconcile(self.store.cancelling()[0])
        self.assertEqual(self.store.own(self.store.user(self.one),job)['state'],'cancelled')

    async def test_zero_matches_finishes_without_signals_and_failure_keeps_job(self):
        job=await self.active_job();await self.act(job,'cancel',self.one)
        async def failed(node,path,data=None):return {'state':'failed','message':'failed fixture'}
        await AutoQueue(self.store,failed).reconcile(self.store.cancelling()[0])
        self.assertEqual(self.store.own(self.store.user(self.one),job)['state'],'active')
        self.owned_targets=[];before=len([c for c in self.calls if c[1]=='/queue/kill'])
        await self.act(job,'finish',self.one)
        self.assertEqual(len([c for c in self.calls if c[1]=='/queue/kill']),before)
        self.assertEqual(self.store.own(self.store.user(self.one),job)['state'],'completed')

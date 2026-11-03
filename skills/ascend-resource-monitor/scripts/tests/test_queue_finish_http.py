"""Finishing must confirm stopping and hold the queue until all targets end."""
import copy,unittest
import test_queue_auto_http as automatic
from queue_auto import AutoQueue
from queue_store import QueueStore


class FinishHTTPTests(unittest.IsolatedAsyncioTestCase):
    auto_match=True
    asyncSetUp=automatic.AutoHTTPTests.asyncSetUp
    asyncTearDown=automatic.AutoHTTPTests.asyncTearDown
    headers=automatic.AutoHTTPTests.headers
    post=automatic.AutoHTTPTests.post
    register=automatic.AutoHTTPTests.register
    act=automatic.AutoHTTPTests.act
    active_job=automatic.AutoHTTPTests.active_job

    async def finish(self,job,mode='process'):
        data={'job':job,'action':'finish','stop':True,'mode':mode}
        preview=await self.post('preview',data,self.one)
        return await self.post('confirm',{**data,'token':preview['token'],'confirmed':True},self.one)

    async def test_both_modes_wait_for_every_target_then_complete_and_release(self):
        original=copy.deepcopy(self.owned_targets)
        for mode in ('process','container'):
            self.owned_targets=copy.deepcopy(original)
            extra=copy.deepcopy(original[0]);extra['container_id']='b'*64;self.owned_targets.append(extra)
            job=await self.active_job()
            waiting=(await self.post('enqueue',{'node':'node-a','title':'next user'},self.two))['job']
            response=await self.finish(job,mode)
            self.assertEqual(response['state'],'pending')
            calls=[c for c in self.calls if c[1]=='/queue/kill'];self.assertEqual(len(calls),2)
            self.assertTrue(all(c[2]['request']['mode']==mode for c in calls))
            self.assertIsNone(self.store.own(self.store.user(self.one),job)['finished'])
            claim={'job':waiting,'action':'claim'}
            blocked=await self.post('preview',claim,self.two)
            await self.post('confirm',{**claim,'token':blocked['token'],'confirmed':True},self.two,expected=409)
            async def pending(node,path,data=None):return {'state':'pending','message':''}
            # Simulate a proxy restart: the completion outcome must be durable.
            reloaded=QueueStore(self.path)
            await AutoQueue(reloaded,pending).reconcile(reloaded.cancelling()[0])
            self.assertEqual(reloaded.own(self.store.user(self.one),job)['state'],'cancelling')
            self.assertFalse(next(t for t in reloaded.view(self.store.user(self.two))['tasks'] if t['id']==waiting)['can_claim'])
            async def done(node,path,data=None):
                return {'targets':[],'blocked':0} if '/owned?' in path else {'state':'succeeded','message':'stopped'}
            await AutoQueue(reloaded,done).reconcile(reloaded.cancelling()[0])
            row=reloaded.own(self.store.user(self.one),job)
            self.assertEqual(row['state'],'completed');self.assertIsNotNone(row['finished'])
            await self.act(waiting,'claim',self.two)
            self.owned_targets=[];await self.act(waiting,'finish',self.two)
            self.calls.clear()

    async def test_failure_remaining_work_and_unknown_result_keep_occupancy(self):
        job=await self.active_job();await self.finish(job)
        async def unavailable(node,path,data=None):raise OSError('fixture unavailable')
        await AutoQueue(self.store,unavailable).reconcile(self.store.cancelling()[0])
        self.assertEqual(self.store.own(self.store.user(self.one),job)['state'],'cancelling')
        async def failed(node,path,data=None):return {'state':'failed','message':'failed'}
        await AutoQueue(self.store,failed).reconcile(self.store.cancelling()[0])
        self.assertEqual(self.store.own(self.store.user(self.one),job)['state'],'active')
        await self.finish(job)
        async def remaining(node,path,data=None):
            return {'targets':self.owned_targets,'blocked':0} if '/owned?' in path else {'state':'succeeded','message':'stopped'}
        await AutoQueue(self.store,remaining).reconcile(self.store.cancelling()[0])
        self.assertEqual(self.store.own(self.store.user(self.one),job)['state'],'active')
        self.assertIsNone(self.store.own(self.store.user(self.one),job)['finished'])

    async def test_explicit_choice_owner_confirmation_and_fresh_targets_required(self):
        job=await self.active_job();data={'job':job,'action':'finish','mode':'process'}
        await self.post('preview',{**data,'stop':True},self.two,expected=403)
        old=await self.post('preview',data,self.one)
        await self.post('confirm',{**data,'token':old['token'],'confirmed':True},self.one,expected=409)
        await self.post('confirm',{**data,'stop':True,'token':old['token'],'confirmed':True},self.one,expected=409)
        data['stop']=True;preview=await self.post('preview',data,self.one)
        await self.post('confirm',{**data,'token':preview['token'],'confirmed':False},self.one,expected=400)
        await self.post('confirm',{**data,'mode':'container','token':preview['token'],'confirmed':True},self.one,expected=409)
        self.owned_targets[0]['processes'][0]['start_ticks']+=1
        await self.post('confirm',{**data,'token':preview['token'],'confirmed':True},self.one,expected=409)
        self.source_failed=True;await self.post('preview',data,self.one,expected=503)
        self.assertFalse(any(c[1]=='/queue/kill' for c in self.calls))

    async def test_no_matching_work_finishes_without_host_stop(self):
        self.owned_targets=[];job=await self.active_job();await self.finish(job,'container')
        self.assertEqual(self.store.own(self.store.user(self.one),job)['state'],'completed')
        self.assertFalse(any(c[1]=='/queue/kill' for c in self.calls))

"""Claim windows enforced through the real queue HTTP API."""
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import asyncio

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
try:
    from aiohttp import web
    from aiohttp.test_utils import TestClient,TestServer
except ImportError:
    raise unittest.SkipTest('Claim HTTP tests require aiohttp')
from queue_http import install_queue,COOKIE


class ClaimHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.now=time.time()
        clock_patch=patch('queue_unified.time.time',return_value=self.now)
        self.clock=clock_patch.start();self.addCleanup(clock_patch.stop)
        async def identity(request):return {'admin':request.headers.get('Authorization')=='fixture-admin','user':{'id':1,'login':'admin'}}
        self.admin=False
        async def admin(request):raise AssertionError('No admin operation expected')
        cfg={'nodes':{'a':{},'b':{}},'audit_log':str(Path(self.temp.name)/'audit.jsonl'),
             'task_queue':{'database':str(Path(self.temp.name)/'queue.db'),'unified_queue':True}}
        app=web.Application();self.store=install_queue(app,cfg,identity,admin)
        self.client=TestClient(TestServer(app));await self.client.start_server()
        self.token=self.store.register('u12345678','Fixture','test-password-only')

    async def asyncTearDown(self):
        await self.client.close();self.temp.cleanup()

    def headers(self):
        result={'Origin':str(self.client.make_url('')).rstrip('/'),'X-A5-Queue':'queue-v1','Cookie':COOKIE+'='+self.token}
        if self.admin:result['Authorization']='fixture-admin'
        return result

    async def post(self,operation,data,status=200):
        r=await self.client.post('/a5-queue/api/'+operation,json=data,headers=self.headers())
        body=await r.json();self.assertEqual(r.status,status,body);return body

    async def state(self):
        r=await self.client.get('/a5-queue/api/state',headers=self.headers())
        return await r.json()

    async def enqueue(self):
        return (await self.post('enqueue',{'machine':'a','cards':8,'requested_seconds':3600,'notifications_ready':True}))['job']

    async def test_claim_requires_current_offer_and_preserves_application_time(self):
        job=await self.enqueue();row=(await self.state())['tasks'][0]
        payload={'job':job,'action':'claim','confirmed':True}
        await self.post('unified-action',payload,409)
        await self.post('unified-action',{**payload,'offer_id':'stale'},409)
        await self.post('unified-action',{**payload,'offer_id':row['offer_id']})
        active=(await self.state())['tasks'][0]
        self.assertEqual(active['state'],'active')
        self.assertEqual(active['created'],row['created'])

    async def test_expired_offer_advances_next_user_and_only_swaps_one_place(self):
        jobs=[await self.enqueue() for _ in range(3)]
        old=(await self.state())['tasks'][0]['offer_id']
        self.clock.return_value=self.now+600
        await self.post('unified-action',{'job':jobs[0],'action':'claim','confirmed':True,'offer_id':old},409)
        rows=(await self.state())['tasks']
        self.assertEqual([r['id'] for r in rows],[jobs[1],jobs[0],jobs[2]])
        self.assertEqual(rows[0]['state'],'ready')
        self.assertEqual(rows[1]['missed_claims'],1)

    async def test_background_scheduler_expires_without_browser_polling(self):
        first=await self.enqueue();second=await self.enqueue()
        self.clock.return_value=self.now+600
        await asyncio.sleep(3.3)
        with self.store.db() as db:
            rows=db.execute('SELECT id,state,missed_claims FROM unified_tasks ORDER BY queue_order').fetchall()
        self.assertEqual([r['id'] for r in rows],[second,first])
        self.assertEqual(rows[0]['state'],'ready')
        self.assertEqual(rows[1]['missed_claims'],1)

    async def test_priority_api_requires_verified_admin(self):
        first=await self.enqueue();row=(await self.state())['tasks'][0]
        await self.post('unified-action',{'job':first,'action':'claim','confirmed':True,'offer_id':row['offer_id']})
        a=await self.enqueue();b=await self.enqueue()
        request={'job':b,'action':'priority-up','confirmed':True,'admin':True}
        await self.post('unified-action',request,403)
        self.admin=True;await self.post('unified-action',request)
        self.assertEqual([r['id'] for r in (await self.state())['tasks']],[first,b,a])


if __name__=='__main__':unittest.main()

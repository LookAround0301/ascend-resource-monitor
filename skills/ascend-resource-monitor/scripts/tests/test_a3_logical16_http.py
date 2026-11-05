"""Use the actual HTTP configuration and serialization with an isolated DB."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer
from queue_http import install_queue,session_cookie_name


class Logical16HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        async def identity(request):return {'admin':False,'user':{}}
        async def forbidden(*args,**kwargs):raise AssertionError('No server control expected')
        cfg={'nodes':{'a':{},'b':{}},'audit_log':str(Path(self.temp.name)/'audit.jsonl'),
             'task_queue':{'database':str(Path(self.temp.name)/'queue.db'),'unified_queue':True,'cards_per_node':16}}
        app=web.Application();self.store=install_queue(app,cfg,identity,forbidden,forbidden)
        self.client=TestClient(TestServer(app));await self.client.start_server()
        token=self.store.register('u12345678','Fixture','fixture-password')
        cookie=session_cookie_name(self.store.session_namespace,self.client.make_url('').authority)
        self.headers={'Origin':str(self.client.make_url('')).rstrip('/'),'X-A5-Queue':'queue-v1','Cookie':cookie+'='+token}

    async def asyncTearDown(self):
        await self.client.close();self.temp.cleanup()

    async def test_dual32_reaches_both_nodes_and_claims_same_offer(self):
        response=await self.client.post('/a5-queue/api/enqueue',headers=self.headers,json={
            'machine':'dual','cards':32,'cards_per_node':16,'requested_seconds':600,'notifications_ready':True})
        self.assertEqual(response.status,200,await response.text())
        state=await (await self.client.get('/a5-queue/api/state',headers=self.headers)).json()
        self.assertEqual(state['cards_per_node'],16)
        row=state['tasks'][0];self.assertEqual(row['allocations'],{n:list(range(16)) for n in ('a','b')})
        response=await self.client.post('/a5-queue/api/unified-action',headers=self.headers,json={
            'job':row['id'],'action':'claim','offer_id':row['offer_id'],'confirmed':True})
        self.assertEqual(response.status,200,await response.text())
        state=await (await self.client.get('/a5-queue/api/state',headers=self.headers)).json()
        self.assertEqual(state['tasks'][0]['state'],'active')

    async def test_single16_and_old_cached_request_rejected(self):
        data={'machine':'a','cards':16,'cards_per_node':16,'requested_seconds':600,'notifications_ready':True}
        response=await self.client.post('/a5-queue/api/enqueue',headers=self.headers,json=data)
        self.assertEqual(response.status,200,await response.text())
        state=await (await self.client.get('/a5-queue/api/state',headers=self.headers)).json()
        self.assertEqual(state['tasks'][0]['allocations'],{'a':list(range(16))})
        del data['cards_per_node'];data['cards']=8
        response=await self.client.post('/a5-queue/api/enqueue',headers=self.headers,json=data)
        self.assertEqual(response.status,409)
        self.assertIn('刷新',(await response.json())['message'])


if __name__=='__main__':unittest.main()

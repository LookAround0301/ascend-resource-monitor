from pathlib import Path
import asyncio
import json
import os
import secrets
import sys
import tempfile
import unittest
sys.path.insert(0,os.environ.get('QUEUE_TEST_RUNTIME',str(Path(__file__).resolve().parents[2]/'assets/runtime')))
from aiohttp import ClientSession,CookieJar,web
from aiohttp.test_utils import TestServer
from queue_http import install_queue

class AgentHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        async def identity(request):return {'admin':False}
        async def forbidden(*args,**kwargs):raise AssertionError('No host control')
        cfg={'nodes':{'A5-118':{},'A5-124':{}},'audit_log':str(Path(self.temp.name)/'audit'),
             'task_queue':{'database':str(Path(self.temp.name)/'queue.db'),'unified_queue':True,'agent_reservations_enabled':True}}
        self.app=web.Application();self.store=install_queue(self.app,cfg,identity,forbidden,forbidden)
        self.server=TestServer(self.app);await self.server.start_server()
        self.session=ClientSession(cookie_jar=CookieJar(unsafe=True))
    async def asyncTearDown(self):
        await self.session.close();await self.server.close();self.temp.cleanup()
    async def post(self,op,data):
        return await self.session.post(self.server.make_url('/a5-queue/api/'+op),json=data,
             headers={'Origin':str(self.server.make_url('')).rstrip('/'),'X-A5-Queue':'queue-v1'})
    async def test_creation_retry_and_receipt_are_scoped_to_logged_in_user(self):
        op=secrets.token_hex(16);body={'operation':op,'request':{'machine':'auto','cards':1,'requested_seconds':1800}}
        r=await self.post('agent-enqueue',body);self.assertEqual(r.status,401)
        r=await self.post('login',{'employee':'a00000001','name':'User One'});self.assertEqual(r.status,200)
        r=await self.post('agent-enqueue',body);self.assertEqual(r.status,200,await r.text());first=await r.json()
        r=await self.post('agent-enqueue',body);self.assertEqual(await r.json(),first)
        r=await self.post('agent-result',{'operation':op});self.assertEqual(await r.json(),first)
        r=await self.post('login',{'employee':'b00000002','name':'User Two'});self.assertEqual(r.status,200)
        r=await self.post('agent-result',{'operation':op});self.assertEqual(r.status,404)
        r=await self.post('agent-enqueue',body);self.assertEqual(r.status,409)
        with self.store.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM unified_tasks').fetchone()[0],1)
    async def test_unknown_fields_and_name_mismatch_rejected(self):
        self.assertEqual((await self.post('login',{'employee':'a00000001','name':'User One'})).status,200)
        body={'operation':secrets.token_hex(16),'request':{'machine':'auto','cards':1,'requested_seconds':1800,'admin':True}}
        self.assertEqual((await self.post('agent-enqueue',body)).status,400)
        self.assertEqual((await self.post('login',{'employee':'a00000001','name':'Other'})).status,401)

    async def test_event_delivery_and_action_replay(self):
        self.assertEqual((await self.post('login',{'employee':'a00000001','name':'User One'})).status,200)
        body={'operation':secrets.token_hex(16),'request':{'machine':'auto','cards':1,'requested_seconds':1800}}
        response=await self.post('agent-enqueue',body);created=await response.json()
        response=await self.post('agent-events',{'after':0,'job':created['job'],'wait_seconds':0})
        events=(await response.json())['events'];self.assertEqual(len(events),1);self.assertEqual(events[0]['state'],'ready')
        operation=secrets.token_hex(16);claim={'operation':operation,'job':created['job'],'action':'claim','offer_id':events[0]['offer_id']}
        response=await self.post('agent-action',claim);self.assertEqual(response.status,200,await response.text());first=await response.json()
        self.assertEqual((await (await self.post('agent-action',claim)).json()),first)
        response=await self.post('agent-events',{'after':events[0]['seq'],'job':created['job'],'wait_seconds':0})
        self.assertEqual((await response.json())['events'][0]['state'],'active')
        await self.post('login',{'employee':'b00000002','name':'User Two'})
        response=await self.post('agent-events',{'after':0,'job':created['job'],'wait_seconds':0})
        self.assertEqual((await response.json())['events'],[])

    async def test_skill_client_create_and_retry_real_http(self):
        script=Path(__file__).resolve().parents[3]/'a5-reservation/scripts/a5_client.py'
        env={**os.environ,'A5_RESERVATION_HOME':str(Path(self.temp.name)/'client'),'PYTHONUTF8':'1'}
        async def run(*args):
            p=await asyncio.create_subprocess_exec(sys.executable,str(script),*args,env=env,
                     stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            out,err=await p.communicate();self.assertEqual(p.returncode,0,err.decode())
            return out.decode()
        await run('configure','--employee','u00000001','--name','Test User',
                  '--url',str(self.server.make_url('')).rstrip('/'))
        output=await run('create','--cards','2','--minutes','30')
        first=json.loads(output.splitlines()[0]);operation=first['operation']
        retried=json.loads((await run('retry',operation)).splitlines()[0])
        self.assertEqual(retried['job'],first['job'])
        self.assertEqual(json.loads((await run('recover',operation)).splitlines()[0])['job'],first['job'])
        self.assertEqual(len(json.loads(await run('query'))['tasks']),1)
        events=json.loads(await run('events','--job',first['job']))['events']
        self.assertEqual(events[0]['state'],'ready')
        claimed=json.loads(await run('action','claim','--job',first['job'],'--offer-id',events[0]['offer_id']))
        self.assertEqual(claimed['reservation']['state'],'active')
        self.assertEqual(json.loads(await run('action-retry',claimed['operation'])),claimed)
        active=json.loads(await run('events','--job',first['job'],'--after',str(events[0]['seq'])))['events']
        self.assertEqual(active[0]['state'],'active')
        finished=json.loads(await run('action','finish','--job',first['job']))
        self.assertEqual(json.loads(await run('action-retry',finished['operation'])),finished)
        self.assertIsNone(finished['reservation'])
        with self.store.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM unified_tasks').fetchone()[0],0)

if __name__=='__main__':unittest.main()

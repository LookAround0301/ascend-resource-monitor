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

    async def test_binding_notice_requires_owner_or_collaborator_and_deduplicates(self):
        job='a'*32;op=secrets.token_hex(16)
        self.assertEqual((await self.post('agent-bound',{'job':job,'operation':op})).status,401)
        await self.post('login',{'employee':'a00000001','name':'User One'})
        created=await (await self.post('agent-enqueue',{'operation':secrets.token_hex(16),
            'request':{'machine':'auto','cards':1,'requested_seconds':1800}})).json()
        data={'job':created['job'],'operation':op}
        self.assertEqual((await self.post('agent-bound',data)).status,200)
        self.assertEqual((await self.post('agent-bound',data)).status,200)
        await self.post('login',{'employee':'b00000002','name':'User Two'})
        self.assertEqual((await self.post('agent-bound',{'job':created['job'],'operation':secrets.token_hex(16)})).status,403)
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM unified_notice_events WHERE kind='agent-bound'").fetchone()[0],1)

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

    async def test_stream_push_and_reconnect_replays_only_after_cursor(self):
        self.assertEqual((await self.post('login',{'employee':'a00000001','name':'User One'})).status,200)
        created=await (await self.post('agent-enqueue',{'operation':secrets.token_hex(16),
            'request':{'machine':'auto','cards':1,'requested_seconds':1800}})).json()
        job=created['job']
        events=(await (await self.post('agent-events',{'job':job,'after':0})).json())['events']
        response=await self.post('agent-stream',{'job':job,'after':events[0]['seq']})
        self.assertEqual(response.status,200)
        self.assertEqual(await asyncio.wait_for(response.content.readline(),3),b': connected\n')
        await response.content.readline()
        claimed=await self.post('agent-action',{'operation':secrets.token_hex(16),'job':job,
                                                 'action':'claim','offer_id':events[0]['offer_id']})
        self.assertEqual(claimed.status,200)
        ident=await asyncio.wait_for(response.content.readline(),5)
        payload=await asyncio.wait_for(response.content.readline(),5)
        self.assertTrue(ident.startswith(b'id: '));active=json.loads(payload.removeprefix(b'data: '))
        self.assertEqual(active['state'],'active');response.close()
        replay=await self.post('agent-stream',{'job':job,'after':events[0]['seq']})
        await asyncio.wait_for(replay.content.readline(),3);await replay.content.readline()
        await asyncio.wait_for(replay.content.readline(),3)
        self.assertEqual(json.loads((await replay.content.readline()).removeprefix(b'data: '))['seq'],active['seq'])
        replay.close()
        await self.post('login',{'employee':'b00000002','name':'User Two'})
        self.assertEqual((await (await self.post('agent-events',{'job':job,'after':0})).json())['events'],[])

    async def test_listener_delivers_and_recovers_cursor_after_restart(self):
        self.assertEqual((await self.post('login',{'employee':'a00000001','name':'User One'})).status,200)
        created=await (await self.post('agent-enqueue',{'operation':secrets.token_hex(16),
            'request':{'machine':'auto','cards':1,'requested_seconds':1800}})).json()
        job=created['job'];root=Path(self.temp.name);received=root/'received.jsonl'
        adapter=root/'adapter.py'
        adapter.write_text('import json,sys\nfrom pathlib import Path\n'
            'event=json.load(sys.stdin)\np=Path('+repr(str(received))+')\n'
            'seen=[json.loads(x)["event_id"] for x in p.read_text().splitlines()] if p.exists() else []\n'
            'if event["event_id"] not in seen:\n'
            ' with p.open("a",encoding="utf-8") as f:f.write(json.dumps(event)+"\\n")\n'
            ' sys.exit(1)\n'
            'print(json.dumps({"accepted":True,"event_id":event["event_id"]}))\n',encoding='utf-8')
        config=root/'adapter.json';config.write_text(json.dumps([sys.executable,str(adapter)]),encoding='utf-8')
        scripts=Path(__file__).resolve().parents[3]/'a5-reservation/scripts'
        env={**os.environ,'A5_RESERVATION_HOME':str(root/'client'),'PYTHONUTF8':'1',
             'A5_EMPLOYEE':'a00000001','A5_NAME':'User One','A5_QUEUE_URL':str(self.server.make_url('')).rstrip('/')}
        async def start():
            return await asyncio.create_subprocess_exec(sys.executable,str(scripts/'a5_push.py'),
                '--job',job,'--session','fixture-session','--adapter',str(config),env=env,
                stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
        async def wait_count(count):
            for _ in range(100):
                if received.exists() and len(received.read_text(encoding='utf-8').splitlines())>=count:return
                await asyncio.sleep(.1)
            self.fail('push adapter did not receive event')
        process=await start()
        try:
            await wait_count(1)
            for _ in range(50):
                if list((root/'client'/'push').glob('*/cursor.json')):break
                await asyncio.sleep(.1)
            else:self.fail('cursor not persisted')
            with self.store.db() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM unified_notice_events WHERE kind='agent-bound'").fetchone()[0],1)
            process.terminate();await process.wait();process=None
            ready=(await (await self.post('agent-events',{'job':job,'after':0})).json())['events'][0]
            self.assertEqual((await self.post('agent-action',{'operation':secrets.token_hex(16),'job':job,
                'action':'claim','offer_id':ready['offer_id']})).status,200)
            process=await start();await wait_count(2)
            all_events=(await (await self.post('agent-events',{'job':job,'after':0})).json())['events']
            active_seq=all_events[-1]['seq']
            for _ in range(100):
                cursors=list((root/'client'/'push').glob('*/cursor.json'))
                if cursors and json.loads(cursors[0].read_text(encoding='utf-8'))['after']>=active_seq:break
                await asyncio.sleep(.1)
            else:self.fail('active event not acknowledged')
            with self.store.db() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM unified_notice_events WHERE kind='agent-bound'").fetchone()[0],2)
            self.assertEqual((await self.post('agent-action',{'operation':secrets.token_hex(16),'job':job,
                'action':'finish'})).status,200)
            await asyncio.wait_for(process.wait(),10);self.assertEqual(process.returncode,0);process=None
            events=[json.loads(line) for line in received.read_text(encoding='utf-8').splitlines()]
            self.assertEqual([e['state'] for e in events],['ready','active','ended'])
            self.assertTrue(all(e['session']=='fixture-session' for e in events))
        finally:
            if process and process.returncode is None:process.terminate();await process.wait()

    async def test_background_listener_binds_owner_and_stops_on_end(self):
        self.assertEqual((await self.post('login',{'employee':'a00000001','name':'User One'})).status,200)
        created=await (await self.post('agent-enqueue',{'operation':secrets.token_hex(16),
            'request':{'machine':'auto','cards':1,'requested_seconds':1800}})).json()
        job=created['job'];root=Path(self.temp.name);received=root/'bg-received.jsonl'
        adapter=root/'bg-adapter.py'
        adapter.write_text('import json,sys\nfrom pathlib import Path\ne=json.load(sys.stdin)\n'
            'with Path('+repr(str(received))+').open("a",encoding="utf-8") as f:f.write(json.dumps(e)+"\\n")\n'
            'print(json.dumps({"accepted":True,"event_id":e["event_id"]}))\n',encoding='utf-8')
        config=root/'bg-adapter.json';config.write_text(json.dumps([sys.executable,str(adapter)]),encoding='utf-8')
        script=Path(__file__).resolve().parents[3]/'a5-reservation/scripts/a5_push.py'
        env={**os.environ,'A5_RESERVATION_HOME':str(root/'bg-client'),'PYTHONUTF8':'1',
             'A5_EMPLOYEE':'a00000001','A5_NAME':'User One','A5_QUEUE_URL':str(self.server.make_url('')).rstrip('/')}
        process=await asyncio.create_subprocess_exec(sys.executable,str(script),'--job',job,'--session','bg-session',
            '--adapter',str(config),'--background',env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        out,err=await process.communicate();self.assertEqual(process.returncode,0,err.decode())
        self.assertTrue(json.loads(out)['started']);self.assertTrue(json.loads(out)['connected'])
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM unified_notice_events WHERE kind='agent-bound'").fetchone()[0],1)
        async def wait_state(state):
            for _ in range(100):
                if received.exists() and any(json.loads(x)['state']==state for x in received.read_text(encoding='utf-8').splitlines()):return
                await asyncio.sleep(.1)
            self.fail('background listener did not receive '+state)
        try:
            await wait_state('ready')
            ready=(await (await self.post('agent-events',{'job':job,'after':0})).json())['events'][0]
            self.assertEqual((await self.post('agent-action',{'operation':secrets.token_hex(16),'job':job,
                'action':'claim','offer_id':ready['offer_id']})).status,200)
        finally:
            await self.post('agent-action',{'operation':secrets.token_hex(16),'job':job,'action':'finish'})
        await wait_state('ended')
        for _ in range(100):
            statuses=list((root/'bg-client'/'push').glob('*/status.json'))
            if statuses and json.loads(statuses[0].read_text(encoding='utf-8'))['state']=='ended':break
            await asyncio.sleep(.1)
        else:self.fail('background listener did not stop after reservation end')

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

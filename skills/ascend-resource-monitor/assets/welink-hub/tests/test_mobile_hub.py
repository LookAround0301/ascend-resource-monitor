from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from aiohttp.test_utils import TestClient,TestServer
from test_http import FakeHub
from mobile_commands import MobileCommands,CommandError


class MobileHubTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.hub=FakeHub(dict(database=str(Path(self.folder.name)/'state.db'),sources=[dict(id='test')],
            routes={'/private':dict(account_cluster='a5',clusters=['a5'],allow_actions=True)},
            mobile_commands=dict(enabled=True)))
        self.client=TestClient(TestServer(self.hub.app()));await self.client.start_server()
        self.job='a'*32;self.other='b'*32;self.executions=[];self.receipts={};self.fail_after_commit=False
        self.hub.cache['a5']=dict(version=1,cluster='a5',label='A5',exported_at=time.time(),
            accounts=['u00000001','a00000001'],tasks=[self.task(self.job)])
        async def backend(payload):
            if payload['mode']=='preview':return dict(job=payload['job'],version='c'*64,state='active',
                allocations={'A5-118':[0]},effect='结束登记，不停止容器。')
            operation=payload['operation']
            if operation not in self.receipts:
                self.executions.append(payload)
                self.receipts[operation]=dict(message='已完成',job=payload['job'],action=payload['action'])
                if self.fail_after_commit:raise RuntimeError('Lost response')
            return self.receipts[operation]
        self.hub.mobile.backend=backend

    async def asyncTearDown(self):await self.client.close();self.folder.cleanup()

    def task(self,job):return dict(id=job,state='active',employee='u00000001',cards=1,allowed_people=[])

    async def post(self,params,employee='u00000001'):
        response=await self.client.post('/private',json=dict(sender=employee,receiver=employee,type='Text',content='/A5预约 '+params))
        self.assertEqual(response.status,200)
        self.assertEqual(await response.json(),{'message':'Plugin executed'})
        await self.hub.mobile.process_pending()

    def code(self):return re.search(r'/A5预约 确认 ([0-9a-f]{8})',self.hub.sent[-1][1]).group(1)

    async def test_claimed_sender_alone_never_executes_and_code_is_private(self):
        await self.post('释放');code=self.code()
        self.assertEqual(self.executions,[])
        self.assertEqual(self.hub.sent[-1][0],'u00000001')
        await self.post('确认 '+code,employee='a00000001');self.assertEqual(self.executions,[])
        await self.post('确认 '+code);self.assertEqual(len(self.executions),1)
        await self.post('确认 '+code);self.assertEqual(len(self.executions),1)

    async def test_query_then_immediate_release_and_confirm_not_blocked_by_query_limit(self):
        await self.post('查询');await self.post('释放 1');code=self.code()
        await self.post('确认 '+code)
        self.assertEqual(len(self.executions),1)

    async def test_no_implicit_all_multiple_tasks_and_old_numbers_do_not_select_new_job(self):
        self.hub.cache['a5']['tasks'].append(self.task(self.other))
        await self.post('释放');self.assertIn('多条',self.hub.sent[-1][1]);self.assertEqual(self.executions,[])
        await self.post('查询')
        self.hub.cache['a5']['tasks']=[self.task(self.other)]
        await self.post('释放 1');self.assertIn('已结束',self.hub.sent[-1][1]);self.assertEqual(self.executions,[])

    async def test_unknown_result_retry_reuses_same_operation(self):
        await self.post('释放');code=self.code();self.fail_after_commit=True
        await self.post('确认 '+code);self.assertIn('已受理',self.hub.sent[-1][1])
        with closing(sqlite3.connect(self.hub.config['database'])) as db,db:db.execute('UPDATE mobile_challenges SET next_attempt=0')
        await self.hub.mobile.process_pending();self.assertIn('已完成',self.hub.sent[-1][1])
        self.assertEqual(len(self.executions),1)

    async def test_unknown_result_cannot_accidentally_start_another_operation(self):
        await self.post('释放');code=self.code();self.fail_after_commit=True
        await self.post('确认 '+code)
        await self.post('释放');self.assertIn('后台自动核对',self.hub.sent[-1][1])
        self.assertEqual(len(self.executions),1)

    async def test_private_code_itself_proves_receipt_if_send_ack_was_lost(self):
        original=self.hub.send
        async def lost_ack(employee,text):
            await original(employee,text)
            raise RuntimeError('Lost delivery acknowledgement')
        self.hub.send=lost_ack
        with self.assertRaises(RuntimeError):await self.hub.mobile.prepare('u00000001','release','')
        code=self.code();self.hub.send=original
        await self.post('确认 '+code);self.assertEqual(len(self.executions),1)

    async def test_expired_code_and_state_change_do_not_mutate(self):
        await self.post('释放');code=self.code()
        with closing(sqlite3.connect(self.hub.config['database'])) as db,db:
            db.execute('UPDATE mobile_challenges SET expires=?',(time.time()-1,))
        await self.post('确认 '+code);self.assertEqual(self.executions,[])

    async def test_trial_route_without_action_permission_cannot_prepare(self):
        self.hub.config['routes']['/private']['allow_actions']=False
        await self.post('释放')
        self.assertNotIn('确认码',self.hub.sent[-1][1]);self.assertEqual(self.executions,[])

    async def test_unconfirmed_operation_recovers_after_restart_without_retyping(self):
        await self.post('释放');code=self.code();self.fail_after_commit=True
        await self.post('确认 '+code)
        backend=self.hub.mobile.backend
        self.hub.mobile=MobileCommands(self.hub);self.hub.mobile.backend=backend
        with closing(sqlite3.connect(self.hub.config['database'])) as db,db:db.execute('UPDATE mobile_challenges SET next_attempt=0')
        await self.hub.mobile.process_pending()
        self.assertEqual(len(self.executions),1)
        self.assertIn('已完成',self.hub.sent[-1][1])

    async def test_retries_before_server_received_request_without_second_confirmation(self):
        backend=self.hub.mobile.backend;attempts=[]
        async def transient(payload):
            if payload['mode']=='execute':
                attempts.append(payload['operation'])
                if len(attempts)<3:raise RuntimeError('Temporary disconnect')
            return await backend(payload)
        self.hub.mobile.backend=transient
        await self.post('释放');code=self.code();await self.post('确认 '+code)
        for _ in range(2):
            with closing(sqlite3.connect(self.hub.config['database'])) as db,db:db.execute('UPDATE mobile_challenges SET next_attempt=0')
            await self.hub.mobile.process_pending()
        self.assertEqual(len(self.executions),1)
        self.assertEqual(len(set(attempts)),1)
        self.assertIn('已完成',self.hub.sent[-1][1])


if __name__=='__main__':unittest.main()

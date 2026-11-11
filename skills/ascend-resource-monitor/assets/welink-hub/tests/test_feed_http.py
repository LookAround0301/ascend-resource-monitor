import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(Path(os.environ.get('QUEUE_TEST_RUNTIME',ROOT.parent/'runtime'))))
sys.path.insert(0,str(ROOT.parent/'runtime'))
from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer
from queue_http import install_queue


class FeedHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder=tempfile.TemporaryDirectory();root=Path(self.folder.name)
        self.token='test-only-'+('a'*64)
        (root/'feed-token').write_text(self.token)
        config=dict(nodes={'A5-118':{},'A5-124':{}},task_queue=dict(
            database=str(root/'queue.db'),unified_queue=True,
            notification_export_token_file=str(root/'feed-token'),mobile_command_token_file=str(root/'feed-token')),
            audit_log=str(root/'audit.jsonl'))
        async def forbidden_identity(*args):raise AssertionError('Private export must not create a session')
        app=web.Application()
        self.store=install_queue(app,config,forbidden_identity,forbidden_identity)
        app.cleanup_ctx.clear()  # Never start scheduling or business-control loops in a test.
        self.client=TestClient(TestServer(app));await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close();self.folder.cleanup()

    async def test_missing_or_wrong_auth_rejected_and_no_forwarded_ip_trust(self):
        for headers in ({},{'Authorization':'Bearer wrong'},
                        {'X-Forwarded-For':'127.0.0.1','Authorization':'Bearer wrong'}):
            response=await self.client.get('/a5-queue/internal/notification-export',headers=headers)
            self.assertEqual(response.status,403)

    async def test_private_export_works_without_sessions_and_cannot_write(self):
        self.store.login_without_password('a00000001','Owner')
        with self.store.db() as db:before=db.execute('SELECT count(*) FROM sessions').fetchone()[0]
        response=await self.client.get('/a5-queue/internal/notification-export',
            headers={'Authorization':'Bearer '+self.token})
        self.assertEqual(response.status,200)
        data=await response.json()
        self.assertEqual(data['version'],1)
        self.assertEqual(data['recipients'],['a00000001'])
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertNotIn(self.token,await response.text())
        self.assertEqual((await self.client.post('/a5-queue/internal/notification-export',json={})).status,405)
        with self.store.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0],before)

    async def test_mobile_endpoint_rejects_missing_auth_and_invalid_operations(self):
        payload=dict(mode='execute',employee='a00000001',job='a'*32,action='release')
        response=await self.client.post('/a5-queue/internal/mobile-command',json=payload)
        self.assertEqual(response.status,403)
        response=await self.client.post('/a5-queue/internal/mobile-command',json=dict(payload,admin=True),
            headers={'Authorization':'Bearer '+self.token})
        self.assertEqual(response.status,400)

    async def test_mobile_private_endpoint_preview_does_not_release_and_execute_once(self):
        import secrets,time
        self.store.login_without_password('a00000001','Owner')
        job=secrets.token_hex(16)
        with self.store.db() as db:
            account=db.execute('SELECT id FROM accounts WHERE employee=?',('a00000001',)).fetchone()[0]
            db.execute('''INSERT INTO unified_tasks (id,account,title,state,created,activated,requested_seconds,cards,allocations)
                VALUES (?,?,?,'active',?,?,?,?,?)''',(job,account,'',time.time()-500,time.time()-60,3600,1,'{"A5-118":[0]}'))
        headers={'Authorization':'Bearer '+self.token}
        payload=dict(mode='preview',employee='a00000001',job=job,action='release')
        response=await self.client.post('/a5-queue/internal/mobile-command',json=payload,headers=headers)
        self.assertEqual(response.status,200);preview=await response.json()
        with self.store.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM unified_tasks').fetchone()[0],1)
        payload.update(mode='execute',operation=secrets.token_hex(16),version=preview['version'],expires=time.time()+300)
        for _ in range(2):
            response=await self.client.post('/a5-queue/internal/mobile-command',json=payload,headers=headers)
            self.assertEqual(response.status,200)
            self.assertIn('已结束登记',(await response.json())['message'])
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM unified_tasks').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM welink_command_receipts').fetchone()[0],1)


if __name__=='__main__':unittest.main()

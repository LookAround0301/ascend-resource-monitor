"""Personal login and fresh write authorization use both authoritative rosters."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))


class WhitelistHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient,TestServer
        import queue_http
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        monitor=patch('queue_monitor.QueueMonitor.poll');monitor.start();self.addCleanup(monitor.stop)
        self.rosters={node:[{'employee':'u12345678','name':'One'}] for node in ('a','b')}
        self.unavailable=False
        async def roster(node,path,data=None):
            self.assertEqual(path,'/roster')
            if self.unavailable:raise OSError('roster unavailable')
            return {'node':node,'people':self.rosters[node]}
        async def identity(request):return {'admin':False,'user':None}
        async def admin(request):raise AssertionError('Unexpected admin endpoint')
        app=web.Application()
        config={'nodes':{'a':{},'b':{}},'audit_log':str(Path(folder.name)/'audit.jsonl'),
                'task_queue':{'database':str(Path(folder.name)/'queue.db'),'unified_queue':True,
                              'strict_nights':True,'monitor_processes':True,'timezone':'Asia/Shanghai',
                              'require_personnel_whitelist':True,'agent_reservations_enabled':True}}
        self.store=queue_http.install_queue(app,config,identity,admin,agent_override=roster)
        self.client=TestClient(TestServer(app));await self.client.start_server()
        self.cookie=queue_http.session_cookie_name(self.store.session_namespace,self.client.make_url('').authority) if hasattr(queue_http,'session_cookie_name') else queue_http.COOKIE
        # Pre-existing sessions from before whitelist enforcement.
        self.one=self.store.register('u12345678','One','fixture-password')
        self.outside=self.store.register('u12345679','Outside','fixture-password')

    async def asyncTearDown(self):await self.client.close()

    def headers(self,token=''):
        return {'Origin':str(self.client.make_url('')).rstrip('/'),'X-A5-Queue':'queue-v1','Cookie':self.cookie+'='+token}

    async def post(self,op,data,token='',status=200):
        response=await self.client.post('/a5-queue/api/'+op,json=data,headers=self.headers(token))
        result=await response.json();self.assertEqual(response.status,status,result);return response,result

    async def state(self,token):
        return await (await self.client.get('/a5-queue/api/state',headers=self.headers(token))).json()

    def payload(self):return {'cards':1,'requested_seconds':3600,'notifications_ready':True,'purpose':'测试'}

    async def test_login_denies_outside_user_without_creating_account_or_session(self):
        response,body=await self.post('login',{'employee':'u12345680','name':'New outsider'},status=403)
        self.assertIn('白名单',body['message']);self.assertNotIn(self.cookie,response.cookies)
        with self.store.db() as db:self.assertIsNone(db.execute('SELECT 1 FROM accounts WHERE employee=?',('u12345680',)).fetchone())
        await self.post('register',{'employee':'u12345680','name':'New outsider'},status=403)

    async def test_login_accepts_normalized_whitelisted_identity(self):
        response,_=await self.post('login',{'employee':' U12345678 ','name':' One '})
        self.assertIn(self.cookie,response.cookies)
        self.assertTrue((await self.state(self.one))['reservation_allowed'])

    async def test_correct_roster_name_repairs_legacy_account_and_preserves_bookings(self):
        _,created=await self.post('enqueue',self.payload(),self.one)
        await self.post('unified-avoid-default',{'avoid_slots':[20,21],'confirmed':True},self.one)
        with self.store.db() as db:
            account=dict(db.execute('SELECT * FROM accounts WHERE employee=?',('u12345678',)).fetchone())
            reservation=dict(db.execute('SELECT * FROM unified_tasks WHERE id=?',(created['job'],)).fetchone())
            preferences=[tuple(row) for row in db.execute('SELECT * FROM unified_user_preferences')]
            db.execute("UPDATE accounts SET name='wrong-pinyin' WHERE id=?",(account['id'],))
        self.assertFalse((await self.state(self.one))['reservation_allowed'])
        await self.post('login',{'employee':'u12345678','name':'wrong-pinyin'},status=403)
        response,_=await self.post('login',{'employee':' U12345678 ','name':' One '})
        new_token=response.cookies[self.cookie].value
        self.assertEqual(self.store.user(new_token)['id'],account['id'])
        self.assertEqual(self.store.user(self.one)['name'],'One')
        state=await self.state(new_token)
        self.assertTrue(state['reservation_allowed'])
        self.assertEqual(state['default_avoid_slots'],[20,21])
        self.assertTrue(next(row for row in state['tasks'] if row['id']==created['job'])['mine'])
        with self.store.db() as db:
            self.assertEqual(dict(db.execute('SELECT * FROM accounts WHERE id=?',(account['id'],)).fetchone()),account)
            self.assertEqual(dict(db.execute('SELECT * FROM unified_tasks WHERE id=?',(created['job'],)).fetchone()),reservation)
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM unified_user_preferences')],preferences)
        await self.post('enqueue',self.payload(),new_token)

    async def test_failed_roster_check_cannot_repair_old_name_or_create_session(self):
        with self.store.db() as db:
            db.execute("UPDATE accounts SET name='old-name' WHERE employee='u12345678'")
            sessions=db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
        for condition in ('wrong-name','missing-node','conflicting-nodes','unavailable'):
            self.rosters['b']=[{'employee':'u12345678','name':'One'}]
            self.unavailable=condition=='unavailable'
            name='Bad' if condition=='wrong-name' else 'One'
            if condition=='missing-node':self.rosters['b']=[]
            if condition=='conflicting-nodes':self.rosters['b'][0]['name']='Another'
            status=503 if condition=='unavailable' else 409 if condition=='conflicting-nodes' else 403
            await self.post('login',{'employee':'u12345678','name':name,'verified_name':name},status=status)
            with self.store.db() as db:
                self.assertEqual(db.execute("SELECT name FROM accounts WHERE employee='u12345678'").fetchone()[0],'old-name')
                self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],sessions)

    async def test_login_rejects_name_mismatch_partial_roster_and_unavailability(self):
        await self.post('login',{'employee':'u12345678','name':'Incorrect'},status=403)
        self.rosters['b']=[]
        await self.post('login',{'employee':'u12345678','name':'One'},status=403)
        self.unavailable=True
        await self.post('login',{'employee':'u12345678','name':'One'},status=503)

    async def test_preexisting_outside_session_cannot_book_or_use_agent_endpoint(self):
        state=await self.state(self.outside)
        self.assertFalse(state['reservation_allowed']);self.assertIn('白名单',state['reservation_block_reason'])
        await self.post('enqueue',self.payload(),self.outside,403)
        await self.post('agent-enqueue',{'operation':'a'*32,'request':{'purpose':'测试预约','cards':1,'requested_seconds':3600}},self.outside,403)
        self.assertEqual((await self.state(self.one))['tasks'],[])

    async def test_removal_blocks_fresh_writes_despite_cached_allowed_status(self):
        _,result=await self.post('enqueue',self.payload(),self.one)
        state=await self.state(self.one);self.assertTrue(state['reservation_allowed'])
        row=next(r for r in state['tasks'] if r['id']==result['job'])
        self.rosters['b']=[]
        await self.post('enqueue',self.payload(),self.one,403)
        await self.post('unified-resources',{'job':row['id'],'offer_id':row['offer_id'],'purpose':'blocked','confirmed':True},self.one,403)
        await self.post('unified-action',{'job':row['id'],'offer_id':row['offer_id'],'action':'claim','confirmed':True},self.one,403)
        await self.post('agent-action',{'job':row['id'],'offer_id':row['offer_id'],'action':'claim','operation':'b'*32},self.one,403)
        self.assertFalse((await self.state(self.one))['reservation_allowed'])
        # Resource release remains available to old sessions.
        await self.post('unified-action',{'job':row['id'],'action':'cancel','confirmed':True},self.one)

    async def test_roster_failure_blocks_writes_but_retains_read_access(self):
        self.unavailable=True
        await self.post('enqueue',self.payload(),self.one,503)
        state=await self.state(self.one)
        self.assertFalse(state['reservation_allowed']);self.assertIn('暂不可用',state['reservation_block_reason'])


if __name__=='__main__':unittest.main()

"""One browser cookie jar, independent queues on the same host/different ports."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
try:
    from aiohttp import ClientSession,CookieJar,web
    from aiohttp.test_utils import TestServer
except ImportError:
    raise unittest.SkipTest('Session isolation HTTP tests require aiohttp')
from queue_http import COOKIE,install_queue,session_cookie_name
from queue_store import QueueStore


class SessionIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.servers=[];self.stores=[];self.paths=[]
        async def identity(request):return {'admin':False}
        async def forbidden(*args,**kwargs):raise AssertionError('No workload control expected')
        for label,capacity in [('a5',8),('a3',16)]:
            path=Path(self.temp.name)/(label+'.db');self.paths.append(path)
            config={'nodes':{'a':{},'b':{}},'audit_log':str(Path(self.temp.name)/(label+'.jsonl')),
                    'task_queue':{'database':str(path),'unified_queue':True,'cards_per_node':capacity}}
            app=web.Application();self.stores.append(install_queue(app,config,identity,forbidden,forbidden))
            server=TestServer(app);await server.start_server();self.servers.append(server)
        # unsafe permits loopback IP cookies, matching a real browser's behavior.
        self.browser=ClientSession(cookie_jar=CookieJar(unsafe=True))

    async def asyncTearDown(self):
        await self.browser.close()
        for server in self.servers:await server.close()
        self.temp.cleanup()

    def name(self,index):
        return session_cookie_name(self.stores[index].session_namespace,self.servers[index].make_url('').authority)

    async def post(self,index,operation,data=None):
        url=self.servers[index].make_url('/a5-queue/api/'+operation)
        return await self.browser.post(url,json=data or {},headers={
            'Origin':str(self.servers[index].make_url('')).rstrip('/'),'X-A5-Queue':'queue-v1'})

    async def login(self,index):
        response=await self.post(index,'login',{'employee':'u12345678','name':'Same Person'})
        self.assertEqual(response.status,200,await response.text())
        self.assertNotIn(COOKIE,response.cookies)
        cookie=response.cookies[self.name(index)]
        self.assertTrue(cookie['httponly']);self.assertEqual(cookie['samesite'],'Strict')
        self.assertEqual(cookie['path'],'/a5-queue')
        return cookie.value

    async def state(self,index):
        response=await self.browser.get(self.servers[index].make_url('/a5-queue/api/state'))
        self.assertEqual(response.status,200,await response.text())
        return await response.json(),response

    async def test_same_person_keeps_both_logins_and_logout_is_local(self):
        first=await self.login(0);second=await self.login(1)
        self.assertNotEqual(self.name(0),self.name(1))
        for index in [0,1,0,1]:
            state,_=await self.state(index)
            self.assertEqual(state['user']['employee'],'u12345678')
            self.assertEqual(state['cards_per_node'],8 if index==0 else 16)
        response=await self.post(1,'logout');self.assertEqual(response.status,200)
        self.assertEqual(response.cookies[self.name(1)].value,'')
        self.assertNotIn(COOKIE,response.cookies)
        self.assertIsNone((await self.state(1))[0]['user'])
        self.assertEqual((await self.state(0))[0]['user']['employee'],'u12345678')
        self.assertIsNotNone(self.stores[0].user(first));self.assertIsNone(self.stores[1].user(second))
        await self.login(1)
        self.assertIsNotNone((await self.state(0))[0]['user'])

    async def test_upgrade_adopts_valid_legacy_cookie_without_overwriting_other_environment(self):
        old=self.stores[1].login_without_password('u12345678','Same Person')
        jar=self.browser.cookie_jar
        jar.update_cookies({COOKIE:old},response_url=self.servers[1].make_url('/a5-queue/api/login'))
        state,response=await self.state(1)
        self.assertEqual(state['user']['employee'],'u12345678')
        self.assertEqual(response.cookies[self.name(1)].value,old)
        self.assertNotIn(COOKIE,response.cookies)
        # An unupgraded A5 server still writes the shared old cookie.
        other=self.stores[0].login_without_password('u12345678','Same Person')
        jar.update_cookies({COOKIE:other},response_url=self.servers[0].make_url('/a5-queue/api/login'))
        self.assertIsNotNone((await self.state(1))[0]['user'])
        await self.post(1,'logout')
        self.assertEqual(jar.filter_cookies(self.servers[0].make_url('/a5-queue/api/state'))[COOKIE].value,other)
        self.assertIsNotNone(self.stores[0].user(other))
        self.assertIsNone((await self.state(1))[0]['user'])

    async def test_logout_cannot_fall_back_to_an_older_valid_login(self):
        older=self.stores[1].login_without_password('u12345678','Same Person')
        self.browser.cookie_jar.update_cookies({COOKIE:older},response_url=self.servers[1].make_url('/a5-queue/api/login'))
        newer=await self.login(1)
        await self.post(1,'logout')
        self.assertIsNone(self.stores[1].user(newer))
        self.assertIsNotNone(self.stores[1].user(older))
        self.assertIsNone((await self.state(1))[0]['user'])

    async def test_foreign_or_invalid_cookie_is_not_accepted_or_migrated(self):
        foreign=self.stores[0].login_without_password('u12345678','Same Person')
        self.browser.cookie_jar.update_cookies({COOKIE:foreign},response_url=self.servers[1].make_url('/a5-queue/api/login'))
        state,response=await self.state(1)
        self.assertIsNone(state['user']);self.assertNotIn(self.name(1),response.cookies)
        valid=self.stores[1].login_without_password('u12345678','Same Person')
        self.browser.cookie_jar.update_cookies({COOKIE:valid,self.name(1):'invalid'},response_url=self.servers[1].make_url('/a5-queue/api/login'))
        self.assertIsNone((await self.state(1))[0]['user'])

    async def test_restart_preserves_scope_and_existing_parallel_sessions(self):
        token=await self.login(1)
        account=self.stores[1].user(token)
        reopened=QueueStore(self.paths[1])
        self.assertEqual(reopened.session_namespace,self.stores[1].session_namespace)
        self.assertEqual(reopened.user(token),account)
        other=reopened.login_without_password('u12345678','Same Person')
        self.assertEqual(reopened.user(token),account);self.assertEqual(reopened.user(other),account)
        self.assertNotEqual(session_cookie_name(reopened.session_namespace,'localhost:13000'),
                            session_cookie_name(reopened.session_namespace,'localhost:13740'))

    async def test_legacy_database_upgrade_preserves_accounts_and_sessions(self):
        token=await self.login(0)
        with self.stores[0].db() as db:
            before={t:[tuple(row) for row in db.execute('SELECT * FROM '+t)] for t in ('accounts','sessions')}
            db.execute('DROP TABLE queue_meta')
        upgraded=QueueStore(self.paths[0])
        self.assertIsNotNone(upgraded.user(token))
        with upgraded.db() as db:
            for table,rows in before.items():self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM '+table)],rows)
        self.assertEqual(QueueStore(self.paths[0]).session_namespace,upgraded.session_namespace)


if __name__=='__main__':unittest.main()

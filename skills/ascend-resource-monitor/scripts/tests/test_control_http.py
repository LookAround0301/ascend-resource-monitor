from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
"""HTTP auth tests with fake Grafana and fake agent: never touches Docker."""
import tempfile
from pathlib import Path
import unittest
try:
    from aiohttp import web
    from aiohttp.test_utils import TestClient,TestServer
except ImportError:
    raise unittest.SkipTest('Optional container-control HTTP tests require aiohttp')
from control_proxy import create_app


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.calls=[]
        async def user(request):
            role=request.headers.get('X-Test-Role','')
            if role not in ('Viewer','Editor','Admin'):raise web.HTTPUnauthorized()
            return web.json_response({'id':1,'login':'test-'+role,'orgId':1,'isGrafanaAdmin':role=='Admin'})
        async def orgs(request):return web.json_response([{'orgId':1,'role':'Viewer'}])
        async def login(request):
            response=web.Response(text='ok');response.set_cookie('one','1');response.set_cookie('two','2');return response
        # The fake backend accepts Basic credentials only as role fixtures.
        async def identity(request):
            value=request.headers.get('Authorization','').removeprefix('Basic ')
            if value not in ('Viewer','Editor','Admin'):raise web.HTTPUnauthorized()
            response=web.json_response({'id':1,'login':'test-'+value,'orgId':1,'isGrafanaAdmin':value=='Admin'})
            response.enable_compression()
            return response
        fake=web.Application();fake.router.add_get('/api/user',identity);fake.router.add_get('/api/user/orgs',orgs);fake.router.add_post('/login',login)
        self.grafana=TestServer(fake);await self.grafana.start_server()
        async def agent(data,actor):self.calls.append((data,actor));return {'stopped':True,'message':'test only'}
        config={'grafana_url':str(self.grafana.make_url('')).rstrip('/'),'org_id':1,
                'audit_log':str(Path(self.temp.name)/'audit.jsonl'),'nodes':{'node-a':{'url':'https://unused'}}}
        self.client=TestClient(TestServer(create_app(config,agent_override=agent)));await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close();await self.grafana.close();self.temp.cleanup()

    def data(self):return {'node':'node-a','target':'a'*64+'@2026-10-31T00:00:00.1Z','confirmed':True}
    def headers(self,role=None):
        h={'Origin':str(self.client.make_url('')).rstrip('/'),'X-A5-Control':'stop-v1'}
        if role:h['Authorization']='Basic '+role
        return h

    async def test_anonymous_viewer_editor_denied_even_with_forged_identity_header(self):
        for role,code in [(None,401),('Viewer',403),('Editor',403)]:
            h=self.headers(role);h['X-WEBAUTH-USER']='admin';h['X-Grafana-Role']='Admin'
            response=await self.client.post('/a5-control/stop',json=self.data(),headers=h)
            self.assertEqual(response.status,code,await response.text())
        self.assertFalse(self.calls)

    async def test_admin_confirmed_request_forwarded_once(self):
        response=await self.client.post('/a5-control/stop',json=self.data(),headers=self.headers('Admin'))
        self.assertEqual(response.status,200,await response.text())
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0][0]['container_id'],'a'*64)
        self.assertEqual(self.calls[0][1]['login'],'test-Admin')

    async def test_admin_cannot_bypass_confirmation_origin_or_target_scope(self):
        cases=[({'confirmed':False},{}),({'node':'elsewhere'},{}),({}, {'Origin':'http://evil.example'}),({}, {'X-A5-Control':''})]
        for data,headers in cases:
            response=await self.client.post('/a5-control/stop',json={**self.data(),**data},headers={**self.headers('Admin'),**headers})
            self.assertIn(response.status,(400,403))
        self.assertFalse(self.calls)

    async def test_admin_identity_does_not_leak_to_next_anonymous_request(self):
        allowed=await self.client.post('/a5-control/stop',json=self.data(),headers=self.headers('Admin'))
        self.assertEqual(allowed.status,200)
        denied=await self.client.post('/a5-control/stop',json=self.data(),headers=self.headers())
        self.assertEqual(denied.status,401)
        self.assertEqual(len(self.calls),1)

    async def test_proxy_forwards_login_cookies_and_get_cannot_stop(self):
        r=await self.client.post('/login');self.assertEqual(r.status,200)
        self.assertEqual(len(r.headers.getall('Set-Cookie')),2)
        r=await self.client.get('/a5-control/stop');self.assertEqual(r.status,404)
        self.assertFalse(self.calls)


if __name__=='__main__':unittest.main()

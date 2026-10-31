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
    raise unittest.SkipTest("Optional HTTP tests require aiohttp")
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
        async def dashboard(request):return web.json_response({'dashboard':{'links':[{'title':'管理员登录','url':'/login'}]}})
        async def dashboard_resource(request):return web.json_response({'apiVersion':'dashboard.grafana.app/v1beta1','spec':{'links':[{'title':'管理员登录','url':'/login'}]}})
        # The fake backend accepts Basic credentials only as role fixtures.
        async def identity(request):
            value=request.headers.get('Authorization','').removeprefix('Basic ')
            if value not in ('Viewer','Editor','Admin'):raise web.HTTPUnauthorized()
            response=web.json_response({'id':1,'login':'test-'+value,'orgId':1,'isGrafanaAdmin':value=='Admin'})
            response.enable_compression()
            return response
        fake=web.Application();fake.router.add_get('/api/user',identity);fake.router.add_get('/api/user/orgs',orgs);fake.router.add_post('/login',login);fake.router.add_get('/api/dashboards/uid/lab',dashboard)
        fake.router.add_get('/apis/dashboard.grafana.app/v1beta1/namespaces/default/dashboards/lab',dashboard_resource)
        self.grafana=TestServer(fake);await self.grafana.start_server()
        async def agent(data,actor):self.calls.append((data,actor));return {'stopped':True,'message':'test only'}
        async def describe(data):return {**data,'container':'test-workload'}
        config={'grafana_url':str(self.grafana.make_url('')).rstrip('/'),'org_id':1,
                'audit_log':str(Path(self.temp.name)/'audit.jsonl'),'nodes':{'node-a':{'url':'https://unused'}}}
        self.client=TestClient(TestServer(create_app(config,agent_override=agent,describe_override=describe)));await self.client.start_server()

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

    async def test_forwarded_proto_header_does_not_spoof_tls_origin(self):
        headers={**self.headers('Admin'),'X-Forwarded-Proto':'https',
                 'Origin':str(self.client.make_url('')).rstrip('/').replace('http://','https://')}
        response=await self.client.post('/a5-control/stop',json=self.data(),headers=headers)
        self.assertEqual(response.status,403)
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

    async def test_confirmation_page_is_admin_only_and_get_never_stops(self):
        params={'node':'node-a','target':self.data()['target']}
        for role,expected in [(None,401),('Viewer',403),('Admin',200)]:
            r=await self.client.get('/a5-control/confirm',params=params,headers=self.headers(role))
            text=await r.text();self.assertEqual(r.status,expected,text)
            self.assertEqual('id="stop-form"' in text,role=='Admin')
        self.assertFalse(self.calls)

    async def test_dashboard_shows_session_without_identity_leak(self):
        for path,key in [('/api/dashboards/uid/lab','dashboard'),('/apis/dashboard.grafana.app/v1beta1/namespaces/default/dashboards/lab','spec')]:
            for role,expected in [('Admin','已登录：test-Admin（管理员）'),('Viewer','已登录：test-Viewer（只读）'),(None,'未登录 · 管理员登录')]:
                r=await self.client.get(path,headers=self.headers(role))
                data=await r.json();self.assertEqual(data[key]['links'][0]['title'],expected)
                self.assertEqual(r.headers['Cache-Control'],'no-store')
                self.assertIn('Cookie',r.headers['Vary'])

    async def test_session_reports_logged_in_readonly_user(self):
        r=await self.client.get('/a5-control/session',headers=self.headers('Viewer'))
        data=await r.json();self.assertTrue(data['authenticated']);self.assertFalse(data['admin'])


if __name__=='__main__':unittest.main()

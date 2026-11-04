"""The unified queue survives default-home aliases and Grafana SPA navigation."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
try:
    from aiohttp import web
    from aiohttp.test_utils import TestClient,TestServer
except ImportError:
    raise unittest.SkipTest('Home navigation HTTP tests require aiohttp')
from control_proxy import create_app


class HomeNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        async def identity(request): raise web.HTTPUnauthorized()
        async def shell(request): return web.Response(text='<html><body>Grafana shell</body></html>',content_type='text/html')
        async def asset(request):
            response=web.Response(text='const fixture = 1;',content_type='application/javascript')
            response.enable_compression();return response
        async def dashboard(request):
            return web.json_response({'dashboard':{'uid':request.match_info['uid'],'links':[],
                'panels':[{'id':1,'title':'A5-118 排队情况','gridPos':{'x':0,'y':4,'w':24,'h':7}}]}})
        async def resource(request):
            return web.json_response({'metadata':{'name':'default-home-dashboard'},'spec':{
                'title':'A5_server','links':[],'elements':{'panel-1':{'kind':'Panel','spec':{'id':1,'title':'A5-118 排队情况'}}},
                'layout':{'kind':'GridLayout','spec':{'items':[{'spec':{'x':0,'y':4,'width':24,'height':7,'element':{'name':'panel-1'}}}]}}}})
        fake=web.Application();fake.router.add_get('/api/user',identity)
        fake.router.add_get('/api/dashboards/uid/{uid}',dashboard)
        fake.router.add_get('/apis/dashboard.grafana.app/v2beta1/namespaces/default/dashboards/default-home-dashboard',resource)
        fake.router.add_get('/public/app.js',asset)
        for route in ['/dashboards','/login','/profile','/d/a5-dual-node','/d/default-home-dashboard/home']:fake.router.add_get(route,shell)
        self.grafana=TestServer(fake);await self.grafana.start_server()
        config={'grafana_url':str(self.grafana.make_url('')).rstrip('/'),'org_id':1,
                'dashboard_uid':'a5-dual-node','nodes':{'A5-118':{'url':'https://unused'},'A5-124':{'url':'https://unused'}},
                'audit_log':str(Path(self.temp.name)/'audit.jsonl'),
                'task_queue':{'database':str(Path(self.temp.name)/'queue.db'),'unified_queue':True}}
        self.client=TestClient(TestServer(create_app(config,agent_override=True)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close();await self.grafana.close();self.temp.cleanup()

    async def test_all_document_entry_points_load_queue_script(self):
        for route in ['/dashboards','/login','/profile','/d/a5-dual-node','/d/default-home-dashboard/home']:
            response=await self.client.get(route,headers={'Accept':'text/html'})
            self.assertEqual(response.status,200)
            self.assertIn('<script src="/a5-queue/home.js" defer></script>',await response.text())

    async def test_default_home_alias_gets_unified_panel_in_both_formats(self):
        response=await self.client.get('/api/dashboards/uid/default-home-dashboard')
        model=(await response.json())['dashboard']
        self.assertIn('排队与资源分配',[p['title'] for p in model['panels']])
        self.assertNotIn('A5-118 排队情况',[p['title'] for p in model['panels']])
        response=await self.client.get('/apis/dashboard.grafana.app/v2beta1/namespaces/default/dashboards/default-home-dashboard')
        model=(await response.json())['spec']
        self.assertIn('排队与资源分配',[p['spec']['title'] for p in model['elements'].values()])

    async def test_other_dashboards_and_javascript_are_unchanged(self):
        response=await self.client.get('/api/dashboards/uid/unrelated')
        model=(await response.json())['dashboard']
        self.assertNotIn('排队与资源分配',[p['title'] for p in model['panels']])
        response=await self.client.get('/public/app.js',headers={'Accept':'application/javascript','Accept-Encoding':'gzip'})
        self.assertEqual(await response.text(),'const fixture = 1;')
        self.assertEqual(response.headers.get('Content-Encoding'),'gzip')

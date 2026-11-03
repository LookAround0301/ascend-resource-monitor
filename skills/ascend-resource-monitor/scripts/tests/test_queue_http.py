"""Real HTTP permission checks against fake Grafana and fake host agents."""
from pathlib import Path
import copy
import sys
import tempfile
import unittest
from urllib.parse import urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
try:
    from aiohttp import web
    from aiohttp.test_utils import TestClient,TestServer
except ImportError:
    raise unittest.SkipTest('HTTP tests require aiohttp')
from control_proxy import create_app
from queue_http import COOKIE
from queue_store import QueueStore

CID='a'*64
START='2026-11-03T00:00:00.000000000Z'


class QueueHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.calls=[]
        async def user(request):
            role=request.headers.get('Authorization','')
            if role not in ('Admin','Viewer'):raise web.HTTPUnauthorized()
            return web.json_response({'id':1,'login':role,'orgId':1,'isGrafanaAdmin':role=='Admin'})
        async def orgs(request):return web.json_response([{'orgId':1,'role':'Viewer'}])
        fake=web.Application();fake.router.add_get('/api/user',user);fake.router.add_get('/api/user/orgs',orgs)
        self.grafana=TestServer(fake);await self.grafana.start_server()
        self.snap={'container_id':CID,'container_started_at':START,'container_changed':False,'container':'fixture','policy':'未标记',
                   'processes':[{'pid':123,'start_ticks':456,'name':'fixture'}]}
        async def agent(node,path,data=None):
            self.calls.append((node,path,copy.deepcopy(data)))
            route=urlsplit(path).path
            if route=='/queue/containers':return [{'id':CID,'name':'fixture','state':'running'}]
            if route=='/queue/snapshot':return copy.deepcopy(self.snap)
            if route=='/queue/kill':return {'state':'pending'}
            if route=='/queue/operation':return {'state':'succeeded','message':'fixture ended'}
            raise AssertionError(path)
        self.path=Path(self.temp.name)/'queue.db'
        cfg={'grafana_url':str(self.grafana.make_url('')).rstrip('/'),'org_id':1,'audit_log':str(Path(self.temp.name)/'audit.jsonl'),
             'nodes':{'node-a':{'url':'https://unused'}},'task_queue':{'database':str(self.path)}}
        self.client=TestClient(TestServer(create_app(cfg,agent_override=True,queue_override=agent)))
        await self.client.start_server();self.store=QueueStore(self.path)
        self.one=await self.register('u12345678');self.two=await self.register('u12345679')

    async def asyncTearDown(self):
        await self.client.close();await self.grafana.close();self.temp.cleanup()

    def headers(self,token=None,admin=False):
        h={'Origin':str(self.client.make_url('')).rstrip('/'),'X-A5-Queue':'queue-v1'}
        if token:h['Cookie']=COOKIE+'='+token
        if admin:h['Authorization']='Admin'
        return h

    async def post(self,operation,data,token=None,admin=False,expected=200,headers=None):
        r=await self.client.post('/a5-queue/api/'+operation,json=data,headers=headers or self.headers(token,admin))
        body=await r.json();self.assertEqual(r.status,expected,body);self.client.session.cookie_jar.clear();return body

    async def register(self,employee):
        r=await self.client.post('/a5-queue/api/register',json={'employee':employee,'name':'Fixture','password':'fixture-password-only'},headers=self.headers())
        self.assertEqual(r.status,200,await r.text());token=r.cookies[COOKIE].value
        self.assertTrue(r.cookies[COOKIE]['httponly']);self.assertEqual(r.cookies[COOKIE]['samesite'],'Strict')
        self.client.session.cookie_jar.clear();return token

    async def job(self):
        await self.post('grant',{'node':'node-a','container_id':CID,'account':self.store.user(self.one)['id'],'confirmed':True},admin=True)
        result=await self.post('enqueue',{'node':'node-a','container_id':CID,'title':'test'},self.one)
        return result['job']

    async def act(self,job,action,token=None,admin=False,mode='process'):
        data={'job':job,'action':action,'mode':mode}
        preview=await self.post('preview',data,token,admin)
        return await self.post('confirm',{**data,'token':preview['token'],'confirmed':True},token,admin)

    async def test_anonymous_and_forged_role_cannot_control(self):
        h={**self.headers(),'X-Grafana-Role':'Admin','X-WEBAUTH-USER':'admin'}
        await self.post('grant',{'confirmed':True,'admin':True},expected=401,headers=h)
        await self.post('grant',{'confirmed':True,'admin':True},self.one,expected=401)
        await self.post('enqueue',{'node':'node-a','container_id':CID,'title':'test','owner':self.store.user(self.one)['id']},self.two,expected=403)
        self.assertEqual(self.calls,[])

    async def test_only_owner_or_real_admin_and_waiting_cancel_never_kills(self):
        job=await self.job();before=len(self.calls)
        await self.post('preview',{'job':job,'action':'cancel'},self.two,expected=403)
        self.assertEqual(len(self.calls),before)
        await self.act(job,'cancel',admin=True)
        self.assertFalse(any(c[1]=='/queue/kill' for c in self.calls))
        self.assertEqual(self.store.own({'id':'admin','admin':True},job)['state'],'cancelled')

    async def test_claim_records_only_and_both_stop_modes(self):
        for mode in ('process','container'):
            job=await self.job();await self.act(job,'claim',self.one)
            self.assertEqual(self.store.own(self.store.user(self.one),job)['state'],'active')
            self.assertFalse(any(c[1]=='/queue/kill' for c in self.calls))
            await self.act(job,'cancel',self.one,mode=mode)
            calls=[c for c in self.calls if c[1]=='/queue/kill'];self.assertEqual(len(calls),1)
            self.assertEqual(calls[0][2]['request']['mode'],mode)
            self.assertEqual(calls[0][2]['actor']['id'],self.store.user(self.one)['id'])
            self.store.resolve_cancel(job,True,'fixture done');self.calls.clear()

    async def test_confirmation_csrf_and_stale_pid(self):
        job=await self.job();await self.act(job,'claim',self.one)
        data={'job':job,'action':'cancel','mode':'process'}
        preview=await self.post('preview',data,self.one)
        confirm={**data,'token':preview['token'],'confirmed':True}
        await self.post('confirm',{**confirm,'confirmed':False},self.one,expected=400)
        await self.post('confirm',confirm,expected=403,headers={**self.headers(self.one),'Origin':'https://elsewhere.invalid'})
        await self.post('confirm',{**confirm,'mode':'container'},self.one,expected=409)
        self.snap['processes'][0]['start_ticks']+=1
        await self.post('confirm',confirm,self.one,expected=409)
        self.assertFalse(any(c[1]=='/queue/kill' for c in self.calls))

    async def test_admin_direct_stop_and_legacy_redirect(self):
        query={'node':'node-a','target':CID+'@'+START}
        r=await self.client.get('/a5-control/confirm',params=query,allow_redirects=False)
        self.assertEqual(r.status,302);self.assertTrue(r.headers['Location'].startswith('/a5-queue/control?'))
        data={**query,'action':'direct','mode':'container'}
        await self.post('preview',data,self.one,expected=401)
        preview=await self.post('preview',data,admin=True)
        await self.post('confirm',{**data,'token':preview['token'],'confirmed':True},admin=True)
        await self.post('confirm',{**data,'token':preview['token'],'confirmed':True},admin=True,expected=409)
        self.assertEqual(len([c for c in self.calls if c[1]=='/queue/kill']),1)

    async def test_public_view_and_protected_admin_list(self):
        await self.job()
        r=await self.client.get('/a5-queue/api/state');data=await r.json()
        self.assertIsNone(data['user']);self.assertEqual(data['grants'],[]);self.assertFalse(data['tasks'][0]['mine'])
        self.assertNotIn('digest',str(data));self.assertNotIn('fixture-password-only',str(data))
        r=await self.client.get('/a5-queue/api/admin',headers=self.headers(self.one));self.assertEqual(r.status,401)
        r=await self.client.get('/a5-queue');self.assertEqual(r.status,200);self.assertIn("frame-ancestors 'none'",r.headers['Content-Security-Policy'])

    async def test_dashboard_control_routes_to_owner_reservation(self):
        job=await self.job();await self.act(job,'claim',self.one)
        for token,admin in [(self.one,False),(None,True)]:
            r=await self.client.get('/a5-queue/api/control-context',params={'node':'node-a','target':CID+'@'+START},headers=self.headers(token,admin))
            self.assertEqual(r.status,200);self.assertEqual(await r.json(),{'job':job,'direct':False})
        r=await self.client.get('/a5-queue/api/control-context',params={'node':'node-a','target':CID+'@'+START},headers=self.headers(self.two))
        self.assertEqual(r.status,403)


if __name__=='__main__':unittest.main()

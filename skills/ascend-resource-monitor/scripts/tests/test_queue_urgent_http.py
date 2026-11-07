"""End-to-end authorization for urgent-user administration and task flags."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
try:
    from aiohttp import web
    from aiohttp.test_utils import TestClient,TestServer
except ImportError:
    raise unittest.SkipTest('Urgent HTTP tests require aiohttp')
from control_common import ControlError
from queue_http import install_queue,session_cookie_name


class UrgentHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.roster={'a':[{'employee':'u12345678','name':'User'}],
                     'b':[{'employee':'u12345678','name':'User'}]}
        async def identity(request):return {'admin':request.headers.get('Authorization')=='fixture-admin','user':{'id':1,'login':'admin'}}
        async def admin(request):
            if request.headers.get('Authorization')!='fixture-admin':raise ControlError(403,'仅管理员')
            return {'id':1,'login':'admin'}
        async def agent(node,path,data=None):return {'node':node,'people':self.roster[node]}
        cfg={'nodes':{'a':{},'b':{}},'audit_log':str(Path(self.temp.name)/'audit.jsonl'),
             'task_queue':{'database':str(Path(self.temp.name)/'queue.db'),'unified_queue':True}}
        app=web.Application();self.store=install_queue(app,cfg,identity,admin,agent)
        self.client=TestClient(TestServer(app));await self.client.start_server()
        self.token=self.store.register('u12345678','User','password-for-fixture')
        self.cookie=session_cookie_name(self.store.session_namespace,self.client.make_url('').authority)

    async def asyncTearDown(self):await self.client.close();self.temp.cleanup()

    def headers(self,admin=False):
        return {'Origin':str(self.client.make_url('')).rstrip('/'),'X-A5-Queue':'queue-v1',
                'Cookie':self.cookie+'='+self.token,**({'Authorization':'fixture-admin'} if admin else {})}

    async def post(self,operation,data,expected=200,headers=None):
        response=await self.client.post('/a5-queue/api/'+operation,json=data,headers=headers or self.headers())
        result=await response.json();self.assertEqual(response.status,expected,result);return result

    async def test_admin_roster_and_personal_emergency_are_enforced(self):
        response=await self.client.get('/a5-queue/api/urgent-users',headers=self.headers())
        self.assertEqual(response.status,403)
        change={'employee':'u12345678','enabled':True,'confirmed':True}
        await self.post('urgent-users',change,403)
        await self.post('urgent-users',change,403,{**self.headers(True),'Origin':'http://other.invalid'})
        self.roster['b']=[]
        await self.post('urgent-users',change,404,self.headers(True))
        self.roster['b']=[{'employee':'u12345678','name':'User'}]
        await self.post('urgent-users',change,200,self.headers(True))
        state=await (await self.client.get('/a5-queue/api/state',headers=self.headers())).json()
        self.assertTrue(state['can_urgent'])
        result=await self.post('enqueue',{'machine':'a','cards':8,'requested_seconds':3600,
                                          'notifications_ready':True,'urgent':True})
        state=await (await self.client.get('/a5-queue/api/state',headers=self.headers())).json()
        self.assertTrue(next(row for row in state['tasks'] if row['id']==result['job'])['urgent'])
        await self.post('urgent-users',{**change,'enabled':False},200,self.headers(True))
        await self.post('enqueue',{'machine':'a','cards':8,'requested_seconds':3600,
                                   'notifications_ready':True,'urgent':True},403)
        await self.post('unified-urgent',{'job':result['job'],'enabled':False,'confirmed':True})
        await self.post('unified-urgent',{'job':result['job'],'enabled':True,'confirmed':True},403)


if __name__=='__main__':unittest.main()

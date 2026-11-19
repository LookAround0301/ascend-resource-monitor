import secrets
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
try:
    from aiohttp import web
    from aiohttp.test_utils import TestClient,TestServer
except ImportError:
    raise unittest.SkipTest('HTTP tests require aiohttp')
from control_common import ControlError
from queue_http import install_queue,session_cookie_name


class EmergencyHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.clock=patch('time.time',return_value=datetime(2026,11,19,10,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp())
        self.clock.start()
        self.calls=[]
        async def identity(request):
            return {'admin':request.headers.get('Authorization')=='fixture-admin','user':{'id':1,'login':'admin'}}
        async def admin(request):
            if request.headers.get('Authorization')!='fixture-admin':raise ControlError(403,'仅管理员')
            return {'id':1,'login':'admin'}
        async def agent(node,path,data=None):
            self.calls.append((node,path,data))
            return {'node':node,'people':[]}
        cfg={'nodes':{'A5-'+str(n):{} for n in (179,183,187,191,220,224,228,232)},
            'audit_log':str(Path(self.tmp.name)/'audit.jsonl'),
            'task_queue':dict(database=str(Path(self.tmp.name)/'queue.db'),unified_queue=True,
                timezone='Asia/Shanghai',strict_nights=True,overtime_control=True,
                midnight_separation=True,night_carryover_cutoff_hour=9,night_takeover_enabled=False,
                normal_max_seconds=28800,admin_preemption_enabled=True,monitor_processes=True)}
        app=web.Application();self.store=install_queue(app,cfg,identity,admin,agent)
        self.client=TestClient(TestServer(app));await self.client.start_server()
        self.token=self.store.login_without_password('a00000001','Fixture User')
        self.cookie=session_cookie_name(self.store.session_namespace,self.client.make_url('').authority)

    async def asyncTearDown(self):
        await self.client.close();self.tmp.cleanup();self.clock.stop()

    def headers(self, admin=False, personal=True):
        return {'Origin':str(self.client.make_url('')).rstrip('/'),'X-A5-Queue':'queue-v1',
                **({'Cookie':self.cookie+'='+self.token} if personal else {}),
                **({'Authorization':'fixture-admin'} if admin else {})}

    async def post(self,operation,data,expected=200,headers=None):
        response=await self.client.post('/a5-queue/api/'+operation,json=data,headers=headers or self.headers())
        result=await response.json();self.assertEqual(response.status,expected,result);return result

    async def add(self):
        return (await self.post('enqueue',dict(machine='multi:8',cards=64,requested_seconds=3600,
                                             notifications_ready=True,avoid_slots=[])))['job']

    async def test_real_admin_required_and_forged_json_and_headers_cannot_preempt(self):
        target=await self.add()
        await self.post('unified-emergency-preview',{'job':target,'admin':True},403)
        await self.post('unified-emergency-preview',{'job':target},403,
                        {**self.headers(),'X-Grafana-Role':'Admin','X-WEBAUTH-USER':'admin'})
        await self.post('unified-emergency-preview',{'job':target},403,
                        {**self.headers(True),'Origin':'https://untrusted.invalid'})
        await self.post('unified-emergency',{'job':target,'confirmed':False},400,self.headers(True))
        self.assertEqual(self.calls,[])

    async def test_admin_without_personal_login_can_preempt_with_exact_once_notifications_and_no_stop(self):
        victim=await self.add()
        with self.store.db() as db:
            offer=db.execute('SELECT offer_id FROM unified_tasks WHERE id=?',(victim,)).fetchone()[0]
        await self.post('unified-action',dict(job=victim,action='claim',offer_id=offer,confirmed=True))
        target=await self.add()
        admin=self.headers(True,False)
        preview=await self.post('unified-emergency-preview',{'job':target},headers=admin)
        self.assertEqual([r['job'] for r in preview['affected']],[victim])
        request=dict(job=target,revision=preview['revision'],operation=secrets.token_hex(16),confirmed=True)
        result=await self.post('unified-emergency',request,headers=admin)
        self.assertEqual(result,await self.post('unified-emergency',request,headers=admin))
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT state FROM unified_tasks WHERE id=?',(victim,)).fetchone()[0],'queued')
            self.assertEqual(db.execute('SELECT state FROM unified_tasks WHERE id=?',(target,)).fetchone()[0],'ready')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM unified_notice_events').fetchone()[0],2)
        self.assertEqual(self.calls,[])


if __name__=='__main__': unittest.main()

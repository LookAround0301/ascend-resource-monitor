"""Claim windows enforced through the real queue HTTP API."""
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import asyncio
from datetime import datetime, timedelta, timezone

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
try:
    from aiohttp import web
    from aiohttp.test_utils import TestClient,TestServer
except ImportError:
    raise unittest.SkipTest('Claim HTTP tests require aiohttp')
from queue_http import install_queue,session_cookie_name


class ClaimHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.now=time.time()
        clock_patch=patch('queue_unified.time.time',return_value=self.now)
        self.clock=clock_patch.start();self.addCleanup(clock_patch.stop)
        async def identity(request):return {'admin':request.headers.get('Authorization')=='fixture-admin','user':{'id':1,'login':'admin'}}
        self.admin=False
        async def admin(request):raise AssertionError('No admin operation expected')
        cfg={'nodes':{'a':{},'b':{}},'audit_log':str(Path(self.temp.name)/'audit.jsonl'),
             'task_queue':{'database':str(Path(self.temp.name)/'queue.db'),'unified_queue':True}}
        self.roster={'a':[{'employee':'u12345679','name':'Other'}],
                     'b':[{'employee':'u12345679','name':'Other'}]}
        async def node_agent(node,path,data=None):
            if path!='/roster' or self.roster[node] is None:raise RuntimeError('roster unavailable')
            return {'node':node,'people':self.roster[node]}
        app=web.Application();self.store=install_queue(app,cfg,identity,admin,node_agent)
        self.client=TestClient(TestServer(app));await self.client.start_server()
        self.cookie=session_cookie_name(self.store.session_namespace,self.client.make_url('').authority)
        self.token=self.store.register('u12345678','Fixture','test-password-only')

    async def asyncTearDown(self):
        await self.client.close();self.temp.cleanup()

    def headers(self):
        result={'Origin':str(self.client.make_url('')).rstrip('/'),'X-A5-Queue':'queue-v1','Cookie':self.cookie+'='+self.token}
        if self.admin:result['Authorization']='fixture-admin'
        return result

    async def post(self,operation,data,status=200):
        r=await self.client.post('/a5-queue/api/'+operation,json=data,headers=self.headers())
        body=await r.json();self.assertEqual(r.status,status,body);return body

    async def state(self):
        r=await self.client.get('/a5-queue/api/state',headers=self.headers())
        return await r.json()

    async def enqueue(self):
        return (await self.post('enqueue',{'machine':'a','cards':8,'requested_seconds':3600,'notifications_ready':True}))['job']

    async def test_passwordless_login_preserves_jobs_and_admin_checks(self):
        job=await self.enqueue()
        response=await self.client.post('/a5-queue/api/login',json={'employee':'U12345678','name':'Fixture','admin':True},headers=self.headers())
        self.assertEqual(response.status,200,await response.text())
        new_token=response.cookies[self.cookie].value
        self.assertEqual(self.store.user(new_token),self.store.user(self.token))
        self.token=new_token
        row=(await self.state())['tasks'][0]
        self.assertEqual(row['id'],job);self.assertTrue(row['mine'])
        await self.post('unified-action',{'job':job,'action':'priority-top','confirmed':True,'admin':True},403)
        await self.post('login',{'employee':'u12345678','name':'Wrong'},401)

    async def test_first_passwordless_login_creates_account(self):
        response=await self.client.post('/a5-queue/api/login',json={'employee':'u12345670','name':'New user'},headers=self.headers())
        self.assertEqual(response.status,200,await response.text())
        person=self.store.user(response.cookies[self.cookie].value)
        self.assertEqual((person['employee'],person['name']),('u12345670','New user'))
        await self.post('login',{'employee':'u12345671','name':''},400)

    async def test_claim_requires_current_offer_and_preserves_application_time(self):
        job=await self.enqueue();row=(await self.state())['tasks'][0]
        self.assertFalse((await self.state())['night_auto_finish_enabled'])
        payload={'job':job,'action':'claim','confirmed':True}
        await self.post('unified-action',payload,409)
        await self.post('unified-action',{**payload,'offer_id':'stale'},409)
        await self.post('unified-action',{**payload,'offer_id':row['offer_id']})
        active=(await self.state())['tasks'][0]
        self.assertEqual(active['state'],'active')
        self.assertEqual(active['created'],row['created'])

    async def test_expired_offer_advances_next_user_and_only_swaps_one_place(self):
        jobs=[await self.enqueue() for _ in range(3)]
        old=(await self.state())['tasks'][0]['offer_id']
        self.clock.return_value=self.now+600
        await self.post('unified-action',{'job':jobs[0],'action':'claim','confirmed':True,'offer_id':old},409)
        rows=(await self.state())['tasks']
        self.assertEqual([r['id'] for r in rows],[jobs[1],jobs[0],jobs[2]])
        self.assertEqual(rows[0]['state'],'ready')
        self.assertEqual(rows[1]['missed_claims'],1)

    async def test_background_scheduler_expires_without_browser_polling(self):
        first=await self.enqueue();second=await self.enqueue()
        self.clock.return_value=self.now+600
        await asyncio.sleep(3.3)
        with self.store.db() as db:
            rows=db.execute('SELECT id,state,missed_claims FROM unified_tasks ORDER BY queue_order').fetchall()
        self.assertEqual([r['id'] for r in rows],[second,first])
        self.assertEqual(rows[0]['state'],'ready')
        self.assertEqual(rows[1]['missed_claims'],1)

    async def test_priority_api_requires_verified_admin(self):
        first=await self.enqueue();row=(await self.state())['tasks'][0]
        await self.post('unified-action',{'job':first,'action':'claim','confirmed':True,'offer_id':row['offer_id']})
        a=await self.enqueue();b=await self.enqueue()
        request={'job':b,'action':'priority-up','confirmed':True,'admin':True}
        await self.post('unified-action',request,403)
        self.admin=True;await self.post('unified-action',request)
        self.assertEqual([r['id'] for r in (await self.state())['tasks']],[first,b,a])
        self.admin=False
        request={'job':a,'action':'priority-top','confirmed':True,'admin':True}
        await self.post('unified-action',request,403)
        self.admin=True;response=await self.post('unified-action',request)
        self.assertIn('置顶',response['message'])
        self.assertEqual([r['id'] for r in (await self.state())['tasks']],[first,a,b])

    async def test_owner_can_defer_ready_via_http_without_admin(self):
        first=await self.enqueue();second=await self.enqueue()
        row=(await self.state())['tasks'][0]
        payload={'job':first,'action':'priority-down','confirmed':True}
        await self.post('unified-action',payload,409)
        await self.post('unified-action',{**payload,'offer_id':row['offer_id']})
        rows=(await self.state())['tasks']
        self.assertEqual([r['id'] for r in rows],[second,first])
        self.assertEqual([r['state'] for r in rows],['ready','queued'])
        await self.post('unified-action',{'job':first,'action':'claim','confirmed':True,'offer_id':row['offer_id']},409)

    async def test_night_duration_is_capped_at_thirteen_hours(self):
        day=datetime.fromtimestamp(self.now,timezone.utc).date()+timedelta(days=1)
        start=datetime.combine(day,datetime.min.time(),timezone.utc).replace(hour=20).timestamp()
        self.clock.return_value=start-3600
        payload={'machine':'a','cards':8,'requested_seconds':13*3600,'notifications_ready':True,
                 'night':True,'not_before':start}
        await self.post('enqueue',payload)
        error=await self.post('enqueue',{**payload,'requested_seconds':13*3600+300},400)
        self.assertIn('13 小时',error['message'])

    async def test_night_over_nine_is_accepted_and_reported_by_api(self):
        day=datetime.fromtimestamp(self.now,timezone.utc).date()+timedelta(days=1)
        start=datetime.combine(day,datetime.min.time(),timezone.utc).replace(hour=21).timestamp()
        self.clock.return_value=start-3600
        payload={'machine':'a','cards':8,'requested_seconds':13*3600,'notifications_ready':True,
                 'night':True,'not_before':start}
        job=(await self.post('enqueue',payload))['job']
        row=next(r for r in (await self.state())['tasks'] if r['id']==job)
        self.assertEqual(row['estimated_start'],start)
        self.assertTrue(row['night_end_overdue'])
        self.assertFalse(row['window_expired'])

    async def test_morning_night_carryover_keeps_times_and_can_finish_via_api(self):
        day=datetime.fromtimestamp(self.now,timezone.utc).date()+timedelta(days=1)
        start=datetime.combine(day,datetime.min.time(),timezone.utc).replace(hour=20).timestamp()
        self.clock.return_value=start-3600
        job=(await self.post('enqueue',{'machine':'a','cards':8,'requested_seconds':13*3600,
                                      'notifications_ready':True,'night':True,'not_before':start}))['job']
        self.clock.return_value=start
        original=next(r for r in (await self.state())['tasks'] if r['id']==job)
        self.clock.return_value=start+13*3600
        row=next(r for r in (await self.state())['tasks'] if r['id']==job)
        self.assertTrue(row['morning_carryover'])
        self.assertEqual((row['activated'],row['requested_seconds']),(original['activated'],original['requested_seconds']))
        await self.post('unified-action',{'job':job,'action':'finish','confirmed':True})
        self.assertFalse((await self.state())['tasks'])

    async def test_standby_notice_is_returned_only_to_owner(self):
        day=datetime.fromtimestamp(self.now,timezone.utc).date()+timedelta(days=1)
        start=datetime.combine(day,datetime.min.time(),timezone.utc).replace(hour=20).timestamp()
        self.clock.return_value=start-3600
        night=(await self.post('enqueue',{'machine':'a','cards':8,'requested_seconds':8*3600,
            'notifications_ready':True,'night':True,'not_before':start}))['job']
        self.clock.return_value=start
        self.assertEqual(next(r for r in (await self.state())['tasks'] if r['id']==night)['state'],'active')
        self.clock.return_value=start+7200
        normal=await self.enqueue()
        self.assertTrue(next(r for r in (await self.state())['tasks'] if r['id']==normal)['normal_start_after_two'])
        self.token=self.store.register('u12345679','Other','test-password-only')
        self.assertFalse(next(r for r in (await self.state())['tasks'] if r['id']==normal)['normal_start_after_two'])

    async def test_allowed_users_api_is_owner_only_and_hidden_from_other_accounts(self):
        job=await self.enqueue()
        match=await self.post('unified-allowed-lookup',{'job':job,'employee':'U12345679'})
        self.assertEqual(match,{'employee':'u12345679','name':'Other'})
        payload={'job':job,'employee':'U12345679','change':'add','confirmed':True,'confirmed_name':'Other'}
        await self.post('unified-allowed',payload)
        self.assertEqual((await self.state())['tasks'][0]['allowed_employees'],['u12345679'])
        owner_token=self.token
        self.token=self.store.register('u12345679','Other','test-password-only')
        shared=(await self.state())['tasks'][0]
        self.assertNotIn('allowed_employees',shared)
        self.assertTrue(shared['collaborator'])
        self.assertEqual(shared['allowed_people'],[{'employee':'u12345679','name':'Other'}])
        await self.post('unified-allowed',payload,403)
        self.admin=True
        await self.post('unified-allowed',payload,403)
        self.admin=False;self.token=owner_token
        await self.post('unified-allowed',{**payload,'change':'remove'})
        self.assertEqual((await self.state())['tasks'][0]['allowed_employees'],[])

    async def test_shared_user_can_claim_and_finish_via_api(self):
        job=await self.enqueue()
        await self.post('unified-allowed',{'job':job,'employee':'u12345679','change':'add','confirmed':True,'confirmed_name':'Other'})
        self.token=self.store.register('u12345679','Other','test-password-only')
        row=(await self.state())['tasks'][0]
        await self.post('unified-action',{'job':job,'action':'cancel','confirmed':True},403)
        await self.post('unified-action',{'job':job,'action':'claim','offer_id':row['offer_id'],'confirmed':True})
        await self.post('unified-action',{'job':job,'action':'finish','confirmed':True})
        self.assertFalse((await self.state())['tasks'])

    async def test_allowed_user_requires_matching_confirmed_whitelist_on_both_nodes(self):
        job=await self.enqueue()
        payload={'job':job,'employee':'u12345679','change':'add','confirmed':True,'confirmed_name':'Other'}
        await self.post('unified-allowed',{**payload,'employee':'u99999999'},404)
        await self.post('unified-allowed',{**payload,'confirmed_name':'Wrong'},409)
        self.roster['b'][0]['name']='Different'
        await self.post('unified-allowed-lookup',{'job':job,'employee':'u12345679'},409)
        await self.post('unified-allowed',payload,409)
        self.roster['b']=None
        await self.post('unified-allowed',payload,503)
        self.assertEqual((await self.state())['tasks'][0]['allowed_employees'],[])

    async def test_allowed_names_are_owner_only_and_use_consistent_rosters(self):
        job=await self.enqueue()
        await self.post('unified-allowed',{'job':job,'employee':'u12345679','change':'add','confirmed':True,'confirmed_name':'Other'})
        self.assertEqual(await self.post('unified-allowed-names',{'job':job}),{'names':{'u12345679':'Other'}})
        self.roster['b'][0]['name']='Changed'
        self.assertEqual(await self.post('unified-allowed-names',{'job':job}),{'names':{}})
        self.roster['b']=None
        await self.post('unified-allowed-names',{'job':job},503)
        self.assertEqual((await self.state())['tasks'][0]['allowed_employees'],['u12345679'])
        self.token=self.store.register('u99999999','Another','test-password-only')
        await self.post('unified-allowed-names',{'job':job},403)
        self.admin=True;await self.post('unified-allowed-names',{'job':job},403)

    async def test_allowed_search_finds_only_consistent_whitelist_entries_for_owner(self):
        job=await self.enqueue()
        self.roster['a'] += [{'employee':'u33345670','name':'Same'}, {'employee':'u33345671','name':'Only A'},
                             {'employee':'u33345672','name':'Different A'}]
        self.roster['b'] += [{'employee':'u33345670','name':'Same'}, {'employee':'u33345672','name':'Different B'}]
        result=await self.post('unified-allowed-search',{'job':job,'query':'4567'})
        self.assertEqual(result['people'],[{'employee':'u12345679','name':'Other'}, {'employee':'u33345670','name':'Same'}])
        await self.post('unified-allowed-search',{'job':job,'query':'45'},400)
        await self.post('unified-allowed-search',{'job':job,'query':'456%'},400)
        self.token=self.store.register('u99999999','Another','test-password-only')
        await self.post('unified-allowed-search',{'job':job,'query':'456'},403)
        self.admin=True
        await self.post('unified-allowed-search',{'job':job,'query':'456'},403)

    async def test_allowed_search_caps_results_and_fails_closed_on_missing_roster(self):
        job=await self.enqueue()
        self.roster['a']=[{'employee':'u777%05d'%i,'name':'Person'} for i in range(12)]
        self.roster['b']=list(self.roster['a'])
        result=await self.post('unified-allowed-search',{'job':job,'query':'U777'})
        self.assertEqual(len(result['people']),8)
        self.roster['b']=None
        await self.post('unified-allowed-search',{'job':job,'query':'777'},503)

    async def test_allowed_search_matches_names_and_preserves_roster_consistency(self):
        job=await self.enqueue()
        self.roster['a'] += [{'employee':'u33345670','name':'张小明'}, {'employee':'u33345671','name':'张小华'},
                             {'employee':'u33345672','name':'张小刚'}, {'employee':'u33345673','name':'Alice Smith'}]
        self.roster['b'] += [{'employee':'u33345670','name':'张小明'}, {'employee':'u33345671','name':'张小华'},
                             {'employee':'u33345672','name':'张小勇'}, {'employee':'u33345673','name':'Alice Smith'}]
        result=await self.post('unified-allowed-search',{'job':job,'query':'张小'})
        self.assertEqual(result['people'],[{'employee':'u33345670','name':'张小明'}, {'employee':'u33345671','name':'张小华'}])
        result=await self.post('unified-allowed-search',{'job':job,'query':'小明'})
        self.assertEqual(result['people'],[{'employee':'u33345670','name':'张小明'}])
        result=await self.post('unified-allowed-search',{'job':job,'query':'ALICE'})
        self.assertEqual(result['people'],[{'employee':'u33345673','name':'Alice Smith'}])
        self.token=self.store.register('u99999999','Another','test-password-only')
        await self.post('unified-allowed-search',{'job':job,'query':'张小'},403)


if __name__=='__main__':unittest.main()

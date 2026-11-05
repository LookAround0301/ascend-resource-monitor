"""Real HTTP checks with fake Grafana and two isolated roster files."""
from pathlib import Path
import json
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'assets/runtime'))
try:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
except ImportError:
    raise unittest.SkipTest('Personnel HTTP tests require aiohttp')
from control_proxy import create_app
from personnel_roster import RosterStore


class PersonnelHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.stores = {}; self.calls = []; self.fail_node = None
        self.guard_states={'node-a':False,'node-b':False}
        for node in ['node-a', 'node-b']:
            path = Path(self.temp.name) / (node + '.json')
            path.write_text(json.dumps({'employee_names': {'u12345678': '原有人员'}, 'containers': {'untouched': 'u12345678'}}), encoding='utf-8')
            engine = SimpleNamespace(config={'node': node, 'guard': {'owners_file': str(path)}},
                                     lock=threading.RLock(), write_audit=lambda event: None)
            self.stores[node] = RosterStore(engine)
        async def user(request):
            role = request.headers.get('Authorization', '').removeprefix('Basic ')
            if role not in ['Admin', 'Viewer', 'Editor']: raise web.HTTPUnauthorized()
            return web.json_response({'id': 1, 'login': role, 'orgId': 1, 'isGrafanaAdmin': role == 'Admin'})
        async def orgs(request): return web.json_response([{'orgId': 1, 'role': 'Viewer'}])
        fake = web.Application(); fake.router.add_get('/api/user', user); fake.router.add_get('/api/user/orgs', orgs)
        self.grafana = TestServer(fake); await self.grafana.start_server()
        async def node_call(node, route, data, actor):
            self.calls.append((node, route, data))
            if route == '/guard':return {'node':node,'enabled':self.guard_states[node],'error':'','entries':[]}
            if route == '/guard/switch':
                if node == self.fail_node:raise OSError('simulated unavailable node')
                self.guard_states[node]=data['enabled'];return {'node':node,'enabled':data['enabled']}
            if route not in ('/roster','/roster/remove'): raise AssertionError(route)
            if data is None: return self.stores[node].snapshot()
            if node == self.fail_node: raise OSError('simulated unavailable node')
            return self.stores[node].remove(data, actor) if route.endswith('/remove') else self.stores[node].add(data, actor)
        config = {'grafana_url': str(self.grafana.make_url('')).rstrip('/'), 'org_id': 1,
                  'nodes': {node: {'url': 'https://unused'} for node in self.stores},
                  'audit_log': str(Path(self.temp.name) / 'audit.jsonl')}
        self.client = TestClient(TestServer(create_app(config, agent_override=True, guard_override=node_call)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close(); await self.grafana.close(); self.temp.cleanup()

    def headers(self, role=None):
        return {'Origin': str(self.client.make_url('')).rstrip('/'), 'X-A5-Control': 'stop-v1',
                **({'Authorization': 'Basic ' + role} if role else {})}

    async def add(self, expected=200, role='Admin', data=None, headers=None, operation='add'):
        payload = {'employee': 'u12345679', 'name': '新人员', 'confirmed': True} if data is None else data
        response = await self.client.post('/a5-control/roster/'+operation, json=payload, headers=headers or self.headers(role))
        result = await response.json(); self.assertEqual(response.status, expected, result); return result

    async def test_page_and_list_are_admin_only(self):
        for role, code in [(None,401),('Viewer',403),('Editor',403)]:
            for path in ['/a5-control/roster','/a5-control/guard']:
                response = await self.client.get(path, headers=self.headers(role))
                self.assertEqual(response.status, code)
                self.assertNotIn('原有人员', await response.text())
        self.assertEqual(self.calls, [])
        for role in ['Admin']:
            response = await self.client.get('/a5-control/roster', headers=self.headers(role)); self.assertEqual(response.status, 200)
            result = await response.json(); self.assertEqual(result['can_edit'], role == 'Admin')
            self.assertEqual(result['people'], [{'employee': 'u12345678', 'name': '原有人员'}])
            response = await self.client.get('/a5-control/guard', headers=self.headers(role)); self.assertEqual(response.status, 200)
            body=await response.text()
            self.assertEqual('id="add-form"' in body, role == 'Admin')
            self.assertNotIn('>管理员登录</a>', body)

    async def test_unauthorized_and_forged_headers_cannot_add(self):
        for role, code in [(None, 401), ('Viewer', 403), ('Editor', 403)]:
            await self.add(expected=code, role=role)
            await self.add(expected=code, role=role, operation='remove')
        await self.add(expected=401, headers={**self.headers(), 'X-Grafana-Role': 'Admin', 'X-WEBAUTH-USER': 'Admin'})
        self.assertFalse(self.calls)

    async def test_confirm_origin_validation_and_name_conflict(self):
        await self.add(expected=403, headers={**self.headers('Admin'), 'Origin': 'http://other.invalid'})
        await self.add(expected=400, data={'employee': 'u12345679', 'name': '名', 'confirmed': False})
        await self.add(expected=400, data={'employee': 'bad', 'name': '名', 'confirmed': True})
        await self.add(expected=409, data={'employee': 'u12345678', 'name': '错误姓名', 'confirmed': True})
        for store in self.stores.values(): self.assertEqual(len(store.snapshot()['people']), 1)

    async def test_two_node_add_and_idempotent_retry(self):
        first = await self.add(); self.assertTrue(all(r['changed'] for r in first['results']))
        second = await self.add(); self.assertFalse(any(r['changed'] for r in second['results']))
        for store in self.stores.values():
            self.assertEqual(len(store.snapshot()['people']), 2)
            self.assertEqual(json.loads(store.path.read_text(encoding='utf-8'))['containers'], {'untouched': 'u12345678'})

    async def test_partial_failure_is_visible_and_retry_completes(self):
        self.fail_node = 'node-b'; result = await self.add(expected=503)
        self.assertIn('node-b', result['message'])
        self.assertEqual(len(self.stores['node-a'].snapshot()['people']), 2)
        self.assertEqual(len(self.stores['node-b'].snapshot()['people']), 1)
        self.fail_node = None
        result = await self.add()
        self.assertEqual([r['changed'] for r in result['results']], [False, True])

    async def test_removal_confirmation_sync_and_retry(self):
        await self.add()
        await self.add(operation='remove', expected=400, data={'employee':'u12345679','name':'新人员','confirmed':False})
        await self.add(operation='remove', expected=403, headers={**self.headers('Admin'),'Origin':'http://other.invalid'})
        self.fail_node='node-b'
        await self.add(operation='remove', expected=503)
        self.assertEqual(len(self.stores['node-a'].snapshot()['people']),1)
        self.fail_node=None
        result=await self.add(operation='remove')
        self.assertEqual([r['changed'] for r in result['results']],[False,True])
        result=await self.add(operation='remove')
        self.assertFalse(any(r['changed'] for r in result['results']))
        for store in self.stores.values(): self.assertEqual(len(store.snapshot()['people']),1)

    async def test_last_entry_cannot_be_removed(self):
        await self.add(operation='remove',expected=409,data={'employee':'u12345678','name':'原有人员','confirmed':True})

    async def test_guard_switch_admin_confirmation_sync_and_partial_failure(self):
        async def switch(role='Admin', enabled=True, confirmed=True, expected=200, headers=None):
            response=await self.client.post('/a5-control/guard/switch',json={'enabled':enabled,'confirmed':confirmed},headers=headers or self.headers(role))
            data=await response.json();self.assertEqual(response.status,expected,data);return data
        for role,code in [(None,401),('Viewer',403),('Editor',403)]:await switch(role=role,expected=code)
        await switch(confirmed=False,expected=400)
        await switch(headers={**self.headers('Admin'),'Origin':'http://other.invalid'},expected=403)
        self.fail_node='node-b';result=await switch(expected=503)
        self.assertIn('node-b',result['message']);self.assertEqual(self.guard_states,{'node-a':True,'node-b':False})
        self.fail_node=None;await switch();self.assertTrue(all(self.guard_states.values()))
        await switch(enabled=False);self.assertFalse(any(self.guard_states.values()))

    async def test_guard_enable_rejects_unsynchronized_rosters_but_can_disable(self):
        self.stores['node-a'].add({'node':'node-a','employee':'u12345679','name':'Extra','revision':self.stores['node-a'].snapshot()['revision'],'confirmed':True},{'id':1,'login':'admin'})
        for enabled,expected in [(True,409),(False,200)]:
            response=await self.client.post('/a5-control/guard/switch',json={'enabled':enabled,'confirmed':True},headers=self.headers('Admin'))
            self.assertEqual(response.status,expected,await response.text())
        self.assertFalse(any(self.guard_states.values()))


if __name__ == '__main__': unittest.main()

"""Restricted, additive personnel roster updates through the existing mTLS agents."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import secrets

from control_common import ControlError


EMPLOYEE = re.compile(r'[a-z][0-9]{8}\Z')


def personnel(employee, name):
    if not isinstance(employee, str) or not isinstance(name, str):
        raise ControlError(400, '请填写工号和姓名')
    employee, name = employee.strip().lower(), name.strip()
    if not EMPLOYEE.fullmatch(employee):
        raise ControlError(400, '工号应为一位字母加八位数字，例如 u12345678')
    if not 1 <= len(name) <= 120 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ControlError(400, '姓名不能为空，也不能包含控制字符')
    return employee, name


class RosterStore:
    def __init__(self, engine):
        self.engine = engine
        self.path = Path(engine.config['guard']['owners_file'])

    def read(self):
        try:
            raw = self.path.read_bytes()
            data = json.loads(raw)
            names = data['employee_names']
            if not isinstance(names, dict) or not names:
                raise ValueError('invalid roster')
            for employee, name in names.items():
                if personnel(employee, name) != (employee, name):
                    raise ValueError('invalid roster entry')
            return raw, data
        except (OSError, ValueError, KeyError, TypeError, ControlError) as e:
            raise ControlError(503, '人员名单暂不可用，请管理员检查原有人员表') from e

    def snapshot(self):
        with self.engine.lock:
            raw, data = self.read()
            return {'node': self.engine.config['node'], 'revision': hashlib.sha256(raw).hexdigest(),
                    'people': [{'employee': employee, 'name': name}
                               for employee, name in sorted(data['employee_names'].items())]}

    def add(self, data, actor):
        return self.change(data, actor, remove=False)

    def remove(self, data, actor):
        return self.change(data, actor, remove=True)

    def change(self, data, actor, remove=False):
        if (not isinstance(data, dict) or set(data) != {'node', 'employee', 'name', 'revision', 'confirmed'}
                or data.get('node') != self.engine.config['node'] or data.get('confirmed') is not True
                or not isinstance(data.get('revision'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', data['revision'])):
            raise ControlError(400, '无效人员名单请求')
        if not isinstance(actor, dict) or not actor.get('id') or not actor.get('login'):
            raise ControlError(403, '缺少经认证的管理员')
        employee, name = personnel(data.get('employee'), data.get('name'))
        with self.engine.lock:
            raw, mapping = self.read()
            existing = mapping['employee_names'].get(employee)
            if (remove and existing is None) or (not remove and existing == name):
                return {'node': data['node'], 'changed': False,
                        'message': '该人员已不在名单中' if remove else '该人员已在名单中'}
            if existing is not None and existing != name:
                raise ControlError(409, '该工号已有不同姓名，请先核实，不会覆盖原记录')
            if remove and len(mapping['employee_names']) <= 1:
                raise ControlError(409, '至少需要保留一位人员，不能清空整个人员表')
            if hashlib.sha256(raw).hexdigest() != data['revision']:
                raise ControlError(409, '人员名单已被更新，请刷新后重试')
            event = {'action': 'personnel.remove' if remove else 'personnel.add', 'node': data['node'], 'actor': actor,
                     'employee': employee, 'name': name}
            self.engine.write_audit({**event, 'result': 'requested'})
            if remove: del mapping['employee_names'][employee]
            else: mapping['employee_names'][employee] = name
            metadata = self.path.stat()
            temporary = self.path.with_name(self.path.name + '.roster-' + secrets.token_hex(8))
            try:
                fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, metadata.st_mode & 0o777)
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump(mapping, stream, ensure_ascii=False, indent=2)
                    stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
                os.chmod(temporary, metadata.st_mode & 0o777)
                if hasattr(os, 'chown'):
                    os.chown(temporary, metadata.st_uid, metadata.st_gid)
                if self.path.read_bytes() != raw:
                    raise ControlError(409, '人员名单已被更新，请刷新后重试')
                os.replace(temporary, self.path)
                if hasattr(os, 'O_DIRECTORY'):
                    parent = os.open(self.path.parent, os.O_DIRECTORY)
                    try: os.fsync(parent)
                    finally: os.close(parent)
            finally:
                if temporary.exists(): temporary.unlink()
            self.engine.write_audit({**event, 'result': 'saved'})
            return {'node': data['node'], 'changed': True, 'message': '人员已删除' if remove else '人员已添加'}


def combine(sources):
    people, issues = {}, []
    for node, source in sources.items():
        if source.get('error'):
            issues.append(node + '：人员名单暂不可用')
            continue
        for row in source['people']:
            people.setdefault(row['employee'], set()).add(row['name'])
    rows = []
    for employee, names in sorted(people.items()):
        if len(names) > 1: issues.append('工号 ' + employee + ' 在两台服务器的姓名不一致')
        rows.append({'employee': employee, 'name': ' / '.join(sorted(names))})
    if not issues and sources:
        sets = [{(row['employee'], row['name']) for row in source['people']} for source in sources.values()]
        if any(rows != sets[0] for rows in sets[1:]): issues.append('两台服务器的人员名单尚未完全同步')
    return {'people': rows, 'issues': issues}


def install_roster(app, config, read_identity, identity, node_call):
    from aiohttp import web
    from control_common import audit, check_browser_request

    async def snapshots():
        async def read(node):
            try:
                result = await node_call(node, '/roster')
                if result.get('node') != node or not re.fullmatch(r'[0-9a-f]{64}', result.get('revision', '')):
                    raise ValueError('invalid source')
                for row in result['people']:
                    if personnel(row['employee'], row['name']) != (row['employee'], row['name']):
                        raise ValueError('invalid source entry')
                return node, result
            except Exception:
                return node, {'error': True}
        return dict(await asyncio.gather(*(read(node) for node in config['nodes'])))

    async def status(request):
        try: await identity(request)
        except ControlError as e:
            return web.json_response({'message': e.message}, status=e.status, headers={'Cache-Control': 'no-store'})
        sources = await snapshots()
        return web.json_response({**combine(sources), 'can_edit': True,
                                  'sources': [{'node': node, 'available': not source.get('error')}
                                              for node, source in sources.items()]},
                                 headers={'Cache-Control': 'no-store', 'Vary': 'Cookie, Authorization'})

    async def add(request):
        try:
            remove = request.path.endswith('/remove')
            check_browser_request(request.headers, request.host, request.scheme)
            actor = await identity(request)
            if request.content_length is None or request.content_length > 4096:
                raise ControlError(400, '无效请求长度')
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {'employee', 'name', 'confirmed'} or data.get('confirmed') is not True:
                raise ControlError(400, '请确认要添加的工号和姓名')
            employee, name = personnel(data['employee'], data['name'])
            sources = await snapshots()
            if not sources or any(source.get('error') for source in sources.values()):
                raise ControlError(503, '请等待两台服务器的名单均可读取后再添加')
            for source in sources.values():
                existing = next((row['name'] for row in source['people'] if row['employee'] == employee), None)
                if existing is not None and existing != name:
                    raise ControlError(409, '该工号已有不同姓名，请核实原记录；此次未修改名单')
                if remove and existing is not None and len(source['people']) <= 1:
                    raise ControlError(409, '至少需要保留一位人员，不能清空整个人员表')
            audit(config['audit_log'], {'action': 'personnel.remove' if remove else 'personnel.add', 'actor': actor,
                                       'employee': employee, 'name': name, 'result': 'authorized'})
            results, failures = [], []
            for node, source in sources.items():
                try:
                    result = await node_call(node, '/roster/remove' if remove else '/roster', {'node': node, 'employee': employee,
                        'name': name, 'revision': source['revision'], 'confirmed': True}, actor)
                    results.append({'node': node, 'changed': result['changed']})
                except Exception: failures.append(node)
            if failures:
                return web.json_response({'message': '部分服务器尚未确认保存：' + '、'.join(failures) +
                    '。请刷新核实后，使用相同工号和姓名重试。', 'results': results},
                    status=503, headers={'Cache-Control': 'no-store'})
            message = ('人员已删除，并已同步到两台服务器' if any(r['changed'] for r in results) else '该人员已不在两台服务器的白名单中') if remove else ('人员已添加，并已同步到两台服务器' if any(r['changed'] for r in results) else '该人员已在两台服务器的白名单中')
            return web.json_response({'message': message, 'results': results}, headers={'Cache-Control': 'no-store'})
        except ControlError as e:
            return web.json_response({'message': e.message}, status=e.status, headers={'Cache-Control': 'no-store'})
        except (ValueError, TypeError, KeyError):
            return web.json_response({'message': '无效人员名单请求'}, status=400)
        except Exception:
            return web.json_response({'message': '保存结果暂不能确认，请刷新名单核实'}, status=503)

    app.router.add_get('/a5-control/roster', status)
    app.router.add_post('/a5-control/roster/add', add)
    app.router.add_post('/a5-control/roster/remove', add)

"""Server-hosted Xiaoluban queries, reminders, and confirmed A5 release/defer."""
import argparse
import asyncio
from contextlib import closing
import json
import logging
from pathlib import Path
import sqlite3
import subprocess
import time

from aiohttp import ClientSession, ClientTimeout, web
from hub_core import allow_query, fresh, init_db, parse_sender, relevant, report, reserve_delivery, resources
from publisher import synchronize
from notification_store import init_notices, reserve_notices, queued, begin_attempt, accepted, rejected
from mobile_commands import MobileCommands,HELP
from group_notifications import initialize as initialize_groups,reserve as reserve_groups,message as group_message,extension_message

LOG = logging.getLogger('welink-hub')


class DeliveryRejected(Exception):
    """A definite platform rejection, with diagnostics that contain no recipient/content."""
    def __init__(self,status,body):
        text=json.dumps(body,ensure_ascii=False).lower()
        category=('throttled' if any(s in text for s in ('频繁','限流','too many','rate limit')) else
                  'recipient-settings' if any(s in text for s in ('未关注','不再接收','拒收','订阅','unsubscribe','blocked')) else
                  'credential' if any(s in text for s in ('token','auth','凭证','认证')) else
                  'recipient' if any(s in text for s in ('收件','用户不存在','工号不存在','invalid user')) else
                  'other')
        self.detail='HTTP%d/%s'%(status,category)
        super().__init__(self.detail)


def load_snapshot(source, config):
    if 'file' in source:
        raw = Path(source['file']).read_bytes()
    else:
        args = ['ssh', '-T', '-F', '/dev/null', '-o', 'BatchMode=yes',
                '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ConnectTimeout=5', '-o', 'UserKnownHostsFile=' + config['known_hosts'],
                '-i', config['ssh_key'], 'ascendwelinkro@' + source['host'], 'snapshot']
        result = subprocess.run(args, capture_output=True, timeout=8, check=True)
        raw = result.stdout
    if len(raw) > 2_000_000:
        raise ValueError('Oversized snapshot')
    snapshot = json.loads(raw)
    if not fresh(snapshot, source['id']):
        raise ValueError('Invalid or stale snapshot')
    return snapshot


class Hub:
    def __init__(self, config):
        self.config = config
        self.cache = {}
        self.health = {}
        self.workers = []
        self.query_lock = asyncio.Lock()
        self.next_publish_check = 0
        init_db(config['database'])
        init_notices(config['database'])
        initialize_groups(config['database'])
        self.mobile=MobileCommands(self) if config.get('mobile_commands',{}).get('enabled') else None

    async def send(self, employee, content):
        token = Path(self.config['token_file']).read_text(encoding='utf-8').strip()
        if not token.startswith(self.config['owner'] + '_'):
            raise ValueError('Credential owner mismatch')
        async with self.http.post(self.config['send_url'], json={
                'content': content, 'receiver': employee, 'auth': token},
                allow_redirects=False) as response:
            body = await response.json(content_type=None)
            if response.status != 200 or not isinstance(body, dict) or body.get('status') != 'ok':
                raise DeliveryRejected(response.status,body)

    async def refresh_one(self, source):
        try:
            self.cache[source['id']] = await asyncio.to_thread(load_snapshot, source, self.config)
            self.health[source['id']] = 'ok'
        except Exception as error:
            self.health[source['id']] = 'unavailable'
            LOG.warning('Source %s unavailable: %s', source['id'], type(error).__name__)

    def snapshot(self, cluster):
        result = self.cache.get(cluster)
        return result if result and fresh(result, cluster) else None

    async def poll(self):
        while True:
            await asyncio.gather(*(self.refresh_one(source) for source in self.config['sources']))
            if self.config.get('published_config') and time.monotonic() >= self.next_publish_check:
                self.next_publish_check = time.monotonic() + 300
                snapshot = self.snapshot('a5')
                if snapshot:
                    try:
                        result = await asyncio.to_thread(synchronize, self.config, snapshot['accounts'])
                        if result:
                            LOG.info('A5 plugin account list synchronized')
                    except Exception as error:
                        LOG.warning('Plugin account synchronization unconfirmed: %s', type(error).__name__)
            await asyncio.sleep(15)

    async def notification_loop(self):
        while True:
            try:
                if self.config.get('reminders_enabled',False):
                    await self.notices()
            except Exception as error:
                LOG.warning('Notification processing failed: %s',type(error).__name__)
            await asyncio.sleep(5)

    async def group_loop(self):
        while True:
            try:await self.group_notices()
            except Exception as error:LOG.warning('Group reminder processing failed: %s',type(error).__name__)
            await asyncio.sleep(5)

    async def group_notices(self):
        config=self.config.get('group_notifications',{})
        if not config.get('enabled') or not self.config.get('reminders_enabled',False):return
        snapshot=self.snapshot(config.get('cluster','a5'))
        if not snapshot:return
        for item in reserve_groups(self.config['database'],snapshot,config):
            content=extension_message(snapshot,item['extension']) if 'extension' in item else group_message(snapshot,item['violation'])
            try:await self.send(item['group'],content)
            except Exception as error:
                LOG.warning('Group reminder delivery unconfirmed: %s',type(error).__name__)
                continue
            with closing(sqlite3.connect(self.config['database'])) as db,db:
                db.execute("UPDATE deliveries SET status='accepted' WHERE event=?",(item['event'],))
            LOG.info('Group %s accepted','extension announcement' if 'extension' in item else 'unauthorized-card reminder')

    async def command_loop(self):
        while True:
            try:await self.mobile.process_pending()
            except Exception as error:LOG.warning('Mobile command reconciliation failed: %s',type(error).__name__)
            await asyncio.sleep(2)

    async def notices(self):
        for source in self.config['sources']:
            if source['id'] not in self.config.get('notification_sources', ['a5']):
                continue
            snapshot = self.snapshot(source['id'])
            if not snapshot:
                continue
            if snapshot.get('notifications_version')==1:
                recipients=set(snapshot.get('notification_recipients',[]))
                if not self.config.get('notification_all_users',False):
                    recipients.intersection_update(self.config.get('notification_users',[]))
                reserve_notices(self.config['database'],snapshot,recipients,
                                self.config.get('notification_events_enabled_at',0),
                                legacy_employee=self.config.get('legacy_reminder_employee'))
                continue
            for row in snapshot['tasks']:
                if row.get('state') != 'ready' or not row.get('offer_id'):
                    continue
                for employee in self.config.get('notification_users', []):
                    if employee not in snapshot['accounts'] or not relevant(row, employee):
                        continue
                    key = reserve_delivery(self.config['database'], source['id'], row, employee,
                                           legacy_employee=self.config.get('legacy_reminder_employee'))
                    if not key:
                        continue
                    remaining = max(0, int((float(row.get('claim_before') or 0) - time.time() + 59) // 60))
                    message = '【%s 排队提醒】你的预约已分配：%s。请在 %d 分钟内到对应集群网页登记开始。' % (snapshot['label'], resources(row), remaining)
                    try:
                        await self.send(employee, message)
                    except Exception as error:
                        LOG.warning('Notification outcome unknown: %s; no automatic resend', type(error).__name__)
                        continue
                    with closing(sqlite3.connect(self.config['database'])) as db, db:
                        db.execute("UPDATE deliveries SET status='accepted' WHERE event=?", (key,))
                    LOG.info('Ready notification accepted for cluster %s', source['id'])
        semaphore=asyncio.Semaphore(4)
        async def deliver(item):
            async with semaphore:
                snapshot=self.snapshot(item['cluster'])
                if not snapshot or item['cluster'] not in self.config.get('notification_sources',['a5']):
                    return
                if item['recipient'] not in snapshot.get('notification_recipients',[]):
                    return
                if not self.config.get('notification_all_users',False) and item['recipient'] not in self.config.get('notification_users',[]):
                    return
                if not begin_attempt(self.config['database'],item['event']):
                    return
                event=item['payload']
                message='【%s %s】\n%s'%(snapshot['label'],event['title'],event['body'])
                try:
                    await self.send(item['recipient'],message)
                except DeliveryRejected as error:
                    rejected(self.config['database'],item['event'])
                    LOG.warning('Notification rejected cluster=%s kind=%s reason=%s',item['cluster'],event['kind'],error.detail)
                    return
                except Exception as error:
                    LOG.warning('Notification outcome unknown: %s; no automatic resend',type(error).__name__)
                    return
                accepted(self.config['database'],item['event'])
                LOG.info('Notification accepted cluster=%s kind=%s',item['cluster'],event['kind'])
        await asyncio.gather(*(deliver(item) for item in queued(self.config['database'])))

    async def callback(self, request):
        route = self.config['routes'].get(request.path)
        if route is None:
            raise web.HTTPNotFound()
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest()
        employee = parse_sender(body)
        if not employee:
            LOG.info('Callback ignored: unsupported message or receiver mismatch')
        permitted = bool(employee and employee in route.get('users', []))
        if employee and route.get('account_cluster'):
            snapshot = self.snapshot(route['account_cluster'])
            permitted = bool(snapshot and employee in snapshot['accounts'])
        if not permitted:
            if employee:
                LOG.info('Callback ignored: caller not in available account cohort')
            return web.json_response({'message': 'Plugin executed'})
        content = body['content'].split(maxsplit=1)
        params = (content[1] if len(content) > 1 else '') if body['content'].startswith('/') else body['content'].strip()
        if self.mobile and route.get('allow_actions') and params.split()[:1] in (['释放'],['延后'],['确认']):
            # The sender claim alone never changes a reservation. An independent
            # code must first be delivered to that same employee in WeLink.
            await self.mobile.handle(employee,params)
            return web.json_response({'message':'Plugin executed'})
        # Never send to a different receiver or expose queue contents/codes in
        # the callback response. Read-only routes cannot execute phone actions.
        async with self.query_lock:
            if not allow_query(self.config['database'], employee):
                LOG.info('Callback ignored: query rate limit')
                return web.json_response({'message': 'Plugin executed'})
        if params not in ('', '查询', '状态'):
            message = HELP if self.mobile and route.get('allow_actions') else '【预约助手】当前入口支持查询自己的预约。'
        else:
            parts = []
            for cluster in route['clusters']:
                snapshot = self.snapshot(cluster)
                if snapshot is None:
                    parts.append('【%s】暂时无法读取队列，请访问该集群预约网页。' % cluster)
                elif employee in snapshot['accounts']:
                    parts.append(report(snapshot, employee,actions=bool(self.mobile and route.get('allow_actions'))))
                    if self.mobile and route.get('allow_actions') and cluster=='a5':
                        self.mobile.remember(employee,[r for r in snapshot['tasks'] if relevant(r,employee)])
                else:
                    parts.append('【%s】你尚未在该集群注册，请先在预约网页登记。' % snapshot['label'])
            message = '\n\n'.join(parts)
        try:
            await self.send(employee, message)
            LOG.info('Private query reply accepted')
        except Exception as error:
            LOG.warning('Private query delivery outcome unknown: %s', type(error).__name__)
            raise web.HTTPServiceUnavailable()
        return web.json_response({'message': 'Plugin executed'})

    async def healthz(self, _request):
        states = {s['id']: 'ok' if self.snapshot(s['id']) else 'unavailable' for s in self.config['sources']}
        return web.json_response({'status': 'ok' if all(v == 'ok' for v in states.values()) else 'degraded',
                                  'sources': states, 'read_only': self.mobile is None,
                                  'mobile_actions':['release','defer'] if self.mobile else []})

    async def start(self, _app):
        self.http = ClientSession(timeout=ClientTimeout(total=8), trust_env=False)
        await asyncio.gather(*(self.refresh_one(source) for source in self.config['sources']))
        self.workers.append(asyncio.create_task(self.poll()))
        self.workers.append(asyncio.create_task(self.notification_loop()))
        if self.mobile:self.workers.append(asyncio.create_task(self.command_loop()))
        if self.config.get('group_notifications',{}).get('enabled'):
            self.workers.append(asyncio.create_task(self.group_loop()))

    async def stop(self, _app):
        for task in self.workers:
            task.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        await self.http.close()

    def app(self):
        @web.middleware
        async def diagnostics(request, handler):
            try:
                response = await handler(request)
            except web.HTTPException as error:
                response = error
            if request.path != '/healthz':
                LOG.info('Callback HTTP method=%s matched=%s status=%d', request.method,
                         request.path in self.config['routes'], response.status)
            return response

        app = web.Application(client_max_size=8192, middlewares=[diagnostics])
        app.router.add_get('/healthz', self.healthz)
        # Register only exact private callback paths. No request/access logging.
        for path in self.config['routes']:
            app.router.add_post(path, self.callback)
        app.on_startup.append(self.start)
        app.on_cleanup.append(self.stop)
        return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding='utf-8'))
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    web.run_app(Hub(config).app(), host=config['bind'], port=config['port'],
                access_log=None, print=None)

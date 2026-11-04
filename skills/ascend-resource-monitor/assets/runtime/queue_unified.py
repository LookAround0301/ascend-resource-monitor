"""Transactional, manual-only reservations across two eight-card nodes."""
import json
import secrets
import time
import math
from datetime import datetime, time as daytime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from control_common import ControlError
from queue_planner import plan


class UnifiedQueue:
    def __init__(self, store, nodes, buffer_seconds=600, timezone='UTC'):
        self.store = store
        self.timezone = dt_timezone.utc if timezone == 'UTC' else ZoneInfo(timezone)
        self.nodes = {node: list(range(8)) for node in nodes}
        if len(self.nodes) != 2:
            raise ValueError('unified queue requires exactly two nodes')
        self.buffer = buffer_seconds
        with store.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS unified_tasks (
                id TEXT PRIMARY KEY, account TEXT NOT NULL, title TEXT NOT NULL,
                state TEXT NOT NULL, created REAL NOT NULL, activated REAL, finished REAL,
                requested_seconds INTEGER NOT NULL, cards INTEGER NOT NULL,
                dual INTEGER NOT NULL DEFAULT 0, not_before REAL NOT NULL DEFAULT 0,
                fixed_node TEXT NOT NULL DEFAULT '', allocations TEXT NOT NULL DEFAULT '{}',
                legacy INTEGER NOT NULL DEFAULT 0, claim_before REAL NOT NULL DEFAULT 0)''')
            if 'claim_before' not in {row['name'] for row in db.execute('PRAGMA table_info(unified_tasks)')}:
                db.execute('ALTER TABLE unified_tasks ADD COLUMN claim_before REAL NOT NULL DEFAULT 0')
            if 'finish_before' not in {row['name'] for row in db.execute('PRAGMA table_info(unified_tasks)')}:
                db.execute('ALTER TABLE unified_tasks ADD COLUMN finish_before REAL NOT NULL DEFAULT 0')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(unified_tasks)')}
            for name, definition in [('queue_order', 'INTEGER NOT NULL DEFAULT 0'),
                                     ('ready_since', 'REAL NOT NULL DEFAULT 0'),
                                     ('offer_id', "TEXT NOT NULL DEFAULT ''"),
                                     ('missed_claims', 'INTEGER NOT NULL DEFAULT 0')]:
                if name not in columns:
                    db.execute('ALTER TABLE unified_tasks ADD COLUMN '+name+' '+definition)
            db.execute('CREATE TABLE IF NOT EXISTS unified_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            if not db.execute("SELECT 1 FROM unified_meta WHERE key='legacy_imported'").fetchone():
                if db.execute("SELECT 1 FROM reservations WHERE state='cancelling' LIMIT 1").fetchone():
                    raise RuntimeError('legacy stop is still pending; resolve it before migration')
                for row in db.execute("SELECT * FROM reservations WHERE state IN ('queued','active') ORDER BY created,rowid").fetchall():
                    if row['node'] not in self.nodes:
                        raise RuntimeError('legacy reservation refers to an unknown node')
                    allocation = {row['node']: self.nodes[row['node']]} if row['state'] == 'active' else {}
                    db.execute('''INSERT INTO unified_tasks
                        (id,account,title,state,created,activated,finished,requested_seconds,cards,
                         fixed_node,allocations,legacy)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,1)''',
                        (row['id'], row['account'], row['title'], row['state'], row['created'],
                         row['activated'], row['finished'], row['requested_seconds'] or 0, 8,
                         row['node'], json.dumps(allocation)))
                db.execute("INSERT INTO unified_meta VALUES ('legacy_imported','1')")
            db.execute("DELETE FROM confirmations WHERE job IN (SELECT id FROM unified_tasks WHERE state IN ('completed','cancelled'))")
            db.execute("DELETE FROM reservations WHERE id IN (SELECT id FROM unified_tasks WHERE state IN ('completed','cancelled'))")
            db.execute("DELETE FROM confirmations WHERE job IN (SELECT id FROM reservations WHERE state IN ('completed','cancelled'))")
            db.execute("DELETE FROM reservations WHERE state IN ('completed','cancelled')")
            db.execute("DELETE FROM unified_tasks WHERE state IN ('completed','cancelled')")
            db.execute("UPDATE unified_tasks SET title='' WHERE title<>''")
            db.execute("UPDATE reservations SET title='' WHERE id IN (SELECT id FROM unified_tasks)")
            db.execute('UPDATE unified_tasks SET queue_order=rowid WHERE queue_order=0')
            # Existing offers get one initial window; subsequent restarts retain it.
            now = time.time()
            for row in db.execute("SELECT * FROM unified_tasks WHERE state='ready' AND (ready_since=0 OR offer_id='')").fetchall():
                deadline = min(now+600, row['claim_before']) if row['claim_before'] else now+600
                db.execute('UPDATE unified_tasks SET ready_since=?,claim_before=?,offer_id=? WHERE id=?',
                           (now, deadline, secrets.token_hex(16), row['id']))

    def cutoff(self, now):
        tomorrow = datetime.fromtimestamp(now, self.timezone).date() + timedelta(days=1)
        return datetime.combine(tomorrow, daytime(9), self.timezone).timestamp()

    def _expire_offers(self, db, now):
        pending = [dict(row) for row in db.execute("SELECT * FROM unified_tasks WHERE state IN ('queued','ready') ORDER BY queue_order,rowid")]
        expired = [row for row in pending if row['state']=='ready' and row['claim_before'] and row['claim_before']<=now]
        missed = {row['id'] for row in expired if row['ready_since'] and now>=row['ready_since']+600}
        for row in expired:
            db.execute("UPDATE unified_tasks SET state='queued',allocations='{}',claim_before=0,ready_since=0,offer_id='',missed_claims=missed_claims+? WHERE id=?",
                       (int(row['id'] in missed), row['id']))
        if not missed:
            return
        # Move each timed-out request back one position, never to the tail.
        # A run of simultaneous misses passes the next request as one block.
        order, i = [], 0
        while i < len(pending):
            if pending[i]['id'] not in missed:
                order.append(pending[i]); i += 1; continue
            start = i
            while i < len(pending) and pending[i]['id'] in missed:
                i += 1
            if i < len(pending):
                order.append(pending[i]); order.extend(pending[start:i]); i += 1
            else:
                order.extend(pending[start:i])
        for position, row in enumerate(order, 1):
            db.execute('UPDATE unified_tasks SET queue_order=? WHERE id=?', (position, row['id']))

    def tick(self):
        with self.store.db() as db:
            self._schedule(db, time.time())

    def _schedule(self, db, now):
        self._expire_offers(db, now)
        rows = [dict(row) for row in db.execute("SELECT * FROM unified_tasks WHERE state IN ('ready','active','queued') ORDER BY queue_order,rowid")]
        busy, waiting = [], []
        for row in rows:
            if row['state'] in ('ready', 'active'):
                end = (row['activated'] + row['requested_seconds'] + self.buffer
                       if row['state'] == 'active' and row['activated'] and row['requested_seconds'] else
                       row['claim_before'] + row['requested_seconds'] + self.buffer
                       if row['state'] == 'ready' and row['claim_before'] else None)
                for node, cards in json.loads(row['allocations']).items():
                    busy.append({'node': node, 'cards': cards, 'start': row['activated'] or row['created'], 'end': end})
            else:
                item = {'id': row['id'], 'cards': row['cards'], 'dual': bool(row['dual']),
                        'seconds': row['requested_seconds'] or 3600, 'not_before': row['not_before'],
                        'finish_before': row['finish_before']}
                if row['fixed_node']:
                    item['allowed_nodes'] = [row['fixed_node']]
                waiting.append(item)
        predictions = {item['id']: item for item in plan(self.nodes, busy, waiting, now, self.buffer)}
        previous=[]
        for row in rows:
            prediction = predictions.get(row['id'])
            if prediction and prediction['start'] is not None and prediction['start'] <= now:
                deadlines=[now+600]
                if row['finish_before']:
                    deadlines.append(row['finish_before'] - row['requested_seconds'])
                for earlier in previous:
                    if earlier['start'] is None or earlier['start'] <= now:
                        continue
                    for node,cards in prediction['allocations'].items():
                        if set(cards).intersection(earlier['allocations'].get(node,[])):
                            deadlines.append(earlier['start'] - (row['requested_seconds'] or 3600) - self.buffer)
                claim_before=min(deadlines)
                db.execute("UPDATE unified_tasks SET state='ready',allocations=?,claim_before=?,ready_since=?,offer_id=? WHERE id=? AND state='queued'",
                           (json.dumps(prediction['allocations']), claim_before, now, secrets.token_hex(16), row['id']))
            if prediction:
                previous.append(prediction)
        return predictions

    def view(self, user):
        now = time.time()
        account = user['id'] if user else ''
        with self.store.db() as db:
            predictions = self._schedule(db, now)
            rows = [dict(row) for row in db.execute('''SELECT r.*,a.employee,a.name FROM unified_tasks r
                JOIN accounts a ON a.id=r.account WHERE r.state IN ('queued','ready','active')
                ORDER BY r.queue_order,r.rowid''')]
        position = 0
        for row in rows:
            row['mine'] = row.pop('account') == account
            row.pop('title', None)
            row['allocations'] = json.loads(row['allocations'])
            if row['state'] in ('queued', 'ready'):
                position += 1
                row['position'] = position
            else:
                row['position'] = None
            row['can_claim'] = row['mine'] and row['state'] == 'ready'
            row['claim_remaining_seconds'] = max(0, row['claim_before']-now) if row['state']=='ready' else 0
            prediction = predictions.get(row['id'], {})
            row['estimated_start'] = prediction.get('start') if row['state'] == 'queued' else None
            row['window_expired'] = bool(row['finish_before'] and now + row['requested_seconds'] > row['finish_before'] and row['state'] != 'active')
            row['planned_allocations'] = prediction.get('allocations', {}) if row['state'] == 'queued' else {}
            row['node'] = ', '.join(row['allocations']) or row['fixed_node'] or '待分配'
            row['machine'] = 'dual' if row['dual'] else row['fixed_node'] or 'auto'
            row['total_cards'] = row['cards'] * (2 if row['dual'] else 1)
        return {'user': user, 'tasks': rows, 'nodes': list(self.nodes), 'server_time': now, 'unified': True,
                'reservation_window': {'start': now, 'end': self.cutoff(now), 'timezone': str(self.timezone)}}

    def enqueue(self, user, data):
        if data.get('notifications_ready') is not True:
            raise ControlError(400, '请先在主页开启桌面通知并通过测试，再加入队列')
        cards, seconds = data.get('cards'), data.get('requested_seconds')
        dual, night, start = data.get('dual', False), data.get('night', False), data.get('not_before', 0)
        now = time.time()
        fixed_node = ''
        if type(dual) is not bool or type(night) is not bool or type(cards) is not int:
            raise ControlError(400, '请选择有效的机器和卡数')
        if 'machine' in data:
            machine = data['machine']
            if not isinstance(machine, str) or machine not in (*self.nodes, 'auto', 'dual'):
                raise ControlError(400, '请选择任意单机、指定单机或双机')
            dual = machine == 'dual'
            if dual:
                if cards not in (2, 4, 8, 16):
                    raise ControlError(400, '双机合计卡数只能选择 2、4、8、16，每台平均分配')
                cards //= 2
            else:
                fixed_node = '' if machine == 'auto' else machine
                if not 1 <= cards <= 8:
                    raise ControlError(400, '单机卡数只能选择 1–8')
        elif not 1 <= cards <= 8 or (dual and cards != 8):
            # Old clients used cards=8, dual=true for two full nodes.
            raise ControlError(400, '旧版请求请使用单机 1–8 卡或双机各 8 卡')
        if type(seconds) is not int or seconds < 60 or seconds % 60 or (not night and seconds > 7200):
            raise ControlError(400, '请按整分钟填写时长，至少 1 分钟；普通任务最长 2 小时')
        if type(start) not in (int, float) or not math.isfinite(start) or start < 0:
            raise ControlError(400, '预约开始时间无效')
        if night and start <= now:
            raise ControlError(400, '夜间预约需填写未来开始时间')
        if not night and start:
            raise ControlError(400, '普通任务无需填写预约开始时间')
        finish_before = self.cutoff(now)
        if max(now, start) + seconds > finish_before:
            raise ControlError(400, '预约开始和预计结束时间都必须在次日上午 9 点前（含 9 点）')
        with self.store.db() as db:
            count = db.execute("SELECT COUNT(*) FROM unified_tasks WHERE account=? AND state IN ('queued','ready','active')", (user['id'],)).fetchone()[0]
            if count >= 5:
                raise ControlError(409, '每人最多保留 5 个未结束预约')
            job = secrets.token_hex(16)
            order = db.execute('SELECT COALESCE(MAX(queue_order),0)+1 FROM unified_tasks').fetchone()[0]
            db.execute('''INSERT INTO unified_tasks(id,account,title,state,created,requested_seconds,cards,dual,not_before,finish_before,fixed_node,queue_order)
                VALUES (?,?,?,'queued',?,?,?,?,?,?,?,?)''', (job, user['id'], '', now, seconds, cards, int(dual), start, finish_before, fixed_node, order))
            self._schedule(db, now)
        return job

    def action(self, user, job, action, offer_id=None):
        if not isinstance(job, str) or action not in ('claim', 'finish', 'cancel'):
            raise ControlError(400, '无效任务操作')
        now = time.time()
        with self.store.db() as db:
            self._schedule(db, now)
            row = db.execute('SELECT * FROM unified_tasks WHERE id=?', (job,)).fetchone()
            if not row:
                raise ControlError(404, '任务不存在')
            if row['account'] != user['id'] and not user.get('admin'):
                raise ControlError(403, '只能操作自己的预约')
            if action == 'claim' and row['state'] == 'ready':
                if offer_id is not None and offer_id != row['offer_id']:
                    raise ControlError(409, '这次资源分配已失效，请刷新后查看当前排队状态')
                if row['finish_before'] and now + row['requested_seconds'] > row['finish_before']:
                    raise ControlError(409, '剩余预约时段不足，请取消后重新预约')
                db.execute("UPDATE unified_tasks SET state='active',activated=? WHERE id=?", (now, job))
            elif action == 'finish' and row['state'] == 'active':
                db.execute('DELETE FROM unified_tasks WHERE id=?', (job,))
                db.execute('DELETE FROM confirmations WHERE job=?', (job,))
                db.execute('DELETE FROM reservations WHERE id=?', (job,))
            elif action == 'cancel' and row['state'] in ('queued', 'ready'):
                db.execute('DELETE FROM unified_tasks WHERE id=?', (job,))
                db.execute('DELETE FROM confirmations WHERE job=?', (job,))
                db.execute('DELETE FROM reservations WHERE id=?', (job,))
            else:
                raise ControlError(409, '任务状态已变化，请刷新后重试')
            self._schedule(db, now)

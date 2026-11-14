"""Transactional, manual-only reservations across two configurable 8/16-card nodes."""
import json
import secrets
import time
import math
import re
from datetime import datetime, time as daytime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from control_common import ControlError
from queue_planner import plan


class UnifiedQueue:
    def __init__(self, store, nodes, buffer_seconds=600, timezone='UTC', daily_reset=False, cards_per_node=8, strict_nights=False, overtime_control=False, normal_priority=False, normal_max_seconds=7200, night_carryover_cutoff_hour=None, missed_claim_limit=0, midnight_separation=False):
        self.store = store
        self.timezone = dt_timezone.utc if timezone == 'UTC' else ZoneInfo(timezone)
        self.daily_reset = daily_reset
        self.strict_nights = strict_nights
        self.overtime_control = bool(strict_nights and (overtime_control or normal_priority))
        self.midnight_separation = bool(midnight_separation and strict_nights)
        self.normal_priority = bool(strict_nights and normal_priority and not self.midnight_separation)
        if midnight_separation and (not strict_nights or not self.overtime_control or night_carryover_cutoff_hour != 9.5):
            raise ValueError('midnight separation requires strict nights, overtime control and a 09:30 night cutoff')
        if type(missed_claim_limit) is not int or not 0<=missed_claim_limit<=100:
            raise ValueError('missed_claim_limit must be an integer from 0 to 100')
        self.missed_claim_limit=missed_claim_limit
        if type(normal_max_seconds) is not int or not 300 <= normal_max_seconds <= 7200 or normal_max_seconds % 300:
            raise ValueError('normal_max_seconds must be a multiple of five minutes, between 5 minutes and 2 hours')
        self.normal_max_seconds = normal_max_seconds
        if night_carryover_cutoff_hour is not None and (type(night_carryover_cutoff_hour) not in (int, float) or not 9<=night_carryover_cutoff_hour<=23 or (night_carryover_cutoff_hour * 60) % 1):
            raise ValueError('night carryover cutoff must be a local hour between 9 and 23')
        if night_carryover_cutoff_hour is not None and not (self.strict_nights and (self.normal_priority or self.midnight_separation)):
            raise ValueError('night carryover cutoff requires strict nights and normal priority')
        self.night_carryover_cutoff_hour = night_carryover_cutoff_hour
        if type(cards_per_node) is not int or cards_per_node not in (8, 16):
            raise ValueError('cards_per_node must be 8 or 16')
        self.capacity = cards_per_node
        self.nodes = {node: list(range(self.capacity)) for node in nodes}
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
            if 'night_fixed' not in {row['name'] for row in db.execute('PRAGMA table_info(unified_tasks)')}:
                db.execute('ALTER TABLE unified_tasks ADD COLUMN night_fixed INTEGER NOT NULL DEFAULT 0')
            if 'night_end_requested' not in {row['name'] for row in db.execute('PRAGMA table_info(unified_tasks)')}:
                db.execute('ALTER TABLE unified_tasks ADD COLUMN night_end_requested REAL NOT NULL DEFAULT 0')
            if 'offer_end_before' not in {row['name'] for row in db.execute('PRAGMA table_info(unified_tasks)')}:
                db.execute('ALTER TABLE unified_tasks ADD COLUMN offer_end_before REAL NOT NULL DEFAULT 0')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(unified_tasks)')}
            for name, definition in [('queue_order', 'INTEGER NOT NULL DEFAULT 0'),
                                     ('ready_since', 'REAL NOT NULL DEFAULT 0'),
                                     ('offer_id', "TEXT NOT NULL DEFAULT ''"),
                                     ('missed_claims', 'INTEGER NOT NULL DEFAULT 0'),
                                     ('allowed_employees', "TEXT NOT NULL DEFAULT '[]'"),
                                     ('avoid_slots', "TEXT NOT NULL DEFAULT '[]'"),
                                     ('backfill_min_seconds', 'INTEGER NOT NULL DEFAULT 0'),
                                     ('backfill_end_before', 'REAL NOT NULL DEFAULT 0'),
                                     ('urgent', 'INTEGER NOT NULL DEFAULT 0'),
                                     ('extension_minutes', 'INTEGER NOT NULL DEFAULT 0'),
                                     ('extension_at', 'REAL NOT NULL DEFAULT 0'),
                                     ('night_requested_start', 'REAL NOT NULL DEFAULT 0'),
                                     ('night_rescheduled_at', 'REAL NOT NULL DEFAULT 0'),
                                     ('night_takeover', 'INTEGER NOT NULL DEFAULT 0'),
                                     ('night_schedule_blocked', 'INTEGER NOT NULL DEFAULT 0'),
                                     ('night_seen_usage', 'INTEGER NOT NULL DEFAULT 0')]:
                if name not in columns:
                    db.execute('ALTER TABLE unified_tasks ADD COLUMN '+name+' '+definition)
            if 'extension_total_minutes' not in columns:
                db.execute('ALTER TABLE unified_tasks ADD COLUMN extension_total_minutes INTEGER NOT NULL DEFAULT 0')
                db.execute('ALTER TABLE unified_tasks ADD COLUMN extension_total_complete INTEGER NOT NULL DEFAULT 1')
                # Older versions saved only the most recent extension. Preserve it
                # as a lower bound; earlier extensions cannot be reconstructed.
                db.execute('UPDATE unified_tasks SET extension_total_minutes=extension_minutes, extension_total_complete=0 WHERE extension_at>0')
            if 'extension_count' not in columns:
                db.execute('ALTER TABLE unified_tasks ADD COLUMN extension_count INTEGER NOT NULL DEFAULT 0')
                # Historical operations were not counted. Existing extensions are
                # known to have happened at least once; do not invent a count.
                db.execute('UPDATE unified_tasks SET extension_count=1 WHERE extension_at>0')
            # A rollback to an older runtime may have performed an extension
            # without updating the new counter; preserve the known lower bound.
            db.execute('UPDATE unified_tasks SET extension_count=1 WHERE extension_at>0 AND extension_count=0')
            db.execute('''CREATE TABLE IF NOT EXISTS unified_notice_events (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, job TEXT NOT NULL,
                employee TEXT NOT NULL, name TEXT NOT NULL, allowed_employees TEXT NOT NULL,
                at REAL NOT NULL, payload TEXT NOT NULL)''')
            db.execute('CREATE TABLE IF NOT EXISTS unified_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS unified_urgent_users (employee TEXT PRIMARY KEY, name TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS unified_night_reclaims (
                id INTEGER PRIMARY KEY,job TEXT NOT NULL,night_job TEXT NOT NULL,at REAL NOT NULL,
                old_allocations TEXT NOT NULL,new_allocations TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS unified_night_reclaims_job ON unified_night_reclaims(job)')
            db.execute('''CREATE TABLE IF NOT EXISTS unified_overtime_releases (
                job TEXT PRIMARY KEY, account TEXT NOT NULL, employee TEXT NOT NULL,
                allowed_employees TEXT NOT NULL, released_at REAL NOT NULL)''')
            if 'reason' not in {row['name'] for row in db.execute('PRAGMA table_info(unified_overtime_releases)')}:
                db.execute("ALTER TABLE unified_overtime_releases ADD COLUMN reason TEXT NOT NULL DEFAULT 'overtime'")
            release_columns={row['name'] for row in db.execute('PRAGMA table_info(unified_overtime_releases)')}
            if 'allocations' not in release_columns:
                db.execute("ALTER TABLE unified_overtime_releases ADD COLUMN allocations TEXT NOT NULL DEFAULT '{}'")
            db.execute('CREATE INDEX IF NOT EXISTS unified_overtime_releases_at ON unified_overtime_releases(released_at)')
            db.execute('CREATE TABLE IF NOT EXISTS unified_continuing (node TEXT, employee TEXT, cutoff REAL, PRIMARY KEY(node,employee))')
            db.execute('CREATE TABLE IF NOT EXISTS unified_shared_continuing (node TEXT, employee TEXT, cutoff REAL, cards TEXT, PRIMARY KEY(node,employee))')
            stored_capacity = db.execute("SELECT value FROM unified_meta WHERE key='cards_per_node'").fetchone()
            existing = (db.execute('SELECT 1 FROM unified_tasks LIMIT 1').fetchone()
                        or db.execute('SELECT 1 FROM unified_shared_continuing LIMIT 1').fetchone())
            previous_capacity = int(stored_capacity[0]) if stored_capacity else (8 if existing else self.capacity)
            if previous_capacity != self.capacity:
                raise ValueError('Queue card capacity changed; migrate reservations before startup')
            db.execute("INSERT OR IGNORE INTO unified_meta VALUES ('cards_per_node',?)", (str(self.capacity),))
            if self.daily_reset:
                db.execute("INSERT OR IGNORE INTO unified_meta VALUES ('reset_checkpoint',?)", (str(self.reset_boundary(time.time())),))
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
                         row['activated'], row['finished'], row['requested_seconds'] or 0, self.capacity,
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
            db.execute('UPDATE unified_tasks SET night_requested_start=not_before WHERE not_before>0 AND night_requested_start=0')
            # Existing offers get one initial window; subsequent restarts retain it.
            now = time.time()
            for row in db.execute("SELECT * FROM unified_tasks WHERE state='ready' AND (ready_since=0 OR offer_id='')").fetchall():
                deadline = min(now+600, row['claim_before']) if row['claim_before'] else now+600
                db.execute('UPDATE unified_tasks SET ready_since=?,claim_before=?,offer_id=? WHERE id=?',
                           (now, deadline, secrets.token_hex(16), row['id']))

    def cutoff(self, now):
        tomorrow = datetime.fromtimestamp(now, self.timezone).date() + timedelta(days=1)
        return datetime.combine(tomorrow, daytime(9), self.timezone).timestamp()

    def night_cutoff(self, start):
        local = datetime.fromtimestamp(start, self.timezone)
        if self.midnight_separation:
            return datetime.combine(local.date(),daytime(9),self.timezone).timestamp()
        day = local.date() if local.hour < 9 else local.date() + timedelta(days=1)
        return datetime.combine(day, daytime(9), self.timezone).timestamp()

    def night_hard_cutoff(self, row):
        if self.night_carryover_cutoff_hour is None or not row.get('not_before'):
            return None
        original=row.get('night_requested_start') or row['not_before']
        nine=row.get('finish_before') or self.night_cutoff(original-0.001)
        local=datetime.fromtimestamp(nine,self.timezone)
        return datetime.combine(local.date(),daytime(int(self.night_carryover_cutoff_hour), int(self.night_carryover_cutoff_hour * 60) % 60),self.timezone).timestamp()

    def normal_two_am(self, created):
        local = datetime.fromtimestamp(created, self.timezone)
        day = local.date() if local.hour < 2 else local.date() + timedelta(days=1)
        return datetime.combine(day, daytime(2), self.timezone).timestamp()

    def normal_one_am(self, created):
        if self.midnight_separation:return self.normal_night_end(created)
        return self.normal_night_end(created)-3600

    def normal_night_end(self, created):
        local = datetime.fromtimestamp(created, self.timezone)
        if self.midnight_separation:
            return datetime.combine(local.date()+timedelta(days=1),daytime.min,self.timezone).timestamp()
        day = local.date() if local.hour < 9 else local.date() + timedelta(days=1)
        return datetime.combine(day, daytime(1 if self.normal_priority else 2), self.timezone).timestamp()

    def normal_allowed_from(self, now):
        local=datetime.fromtimestamp(now,self.timezone)
        boundary=datetime.combine(local.date(),daytime(9,30),self.timezone).timestamp()
        return max(now,boundary) if self.midnight_separation else now

    @staticmethod
    def validate_avoid_slots(value):
        if not isinstance(value,list) or len(value)>48 or any(type(slot) is not int or not 0<=slot<48 for slot in value):
            raise ControlError(400,'避开时段必须是有效的半小时时段')
        return sorted(set(value))

    def avoid_windows(self, slots, now):
        day=datetime.fromtimestamp(now,self.timezone).date()
        return [(datetime.combine(day+timedelta(days=offset),daytime(slot//2,(slot%2)*30),self.timezone).timestamp(),
                 datetime.combine(day+timedelta(days=offset),daytime(slot//2,(slot%2)*30),self.timezone).timestamp()+1800)
                for offset in range(-1,3) for slot in slots]

    def night_windows(self, now):
        day = datetime.fromtimestamp(now, self.timezone).date()
        result = []
        if self.midnight_separation:
            for date in (day,day+timedelta(days=1)):
                start=datetime.combine(date,daytime.min,self.timezone).timestamp()
                end=datetime.combine(date,daytime(9,30),self.timezone).timestamp()
                if end>now:result.append({'start':max(start,now),'end':end})
            return result
        for date in (day - timedelta(days=1), day):
            start = datetime.combine(date, daytime(20), self.timezone).timestamp()
            end = datetime.combine(date + timedelta(days=1), daytime(9), self.timezone).timestamp()
            if end > now:
                result.append({'start': max(start, now), 'end': end})
        return result

    def reset_boundary(self, now):
        current = datetime.fromtimestamp(now, self.timezone)
        day = current.date() if current.hour >= 9 else current.date()-timedelta(days=1)
        return datetime.combine(day, daytime(9), self.timezone).timestamp()

    def _reset_if_due(self, db, now):
        if not self.daily_reset:
            return
        if self.midnight_separation:
            # Overnight submissions must still be queued when normal starts
            # reopen at 09:30. Expired active reservations are handled below.
            return
        boundary = self.reset_boundary(now)
        previous = db.execute("SELECT value FROM unified_meta WHERE key='reset_checkpoint'").fetchone()
        if previous and float(previous[0]) >= boundary:
            return
        count = db.execute('SELECT COUNT(*) FROM unified_tasks WHERE not_before=0').fetchone()[0]
        shared={}
        for row in db.execute("SELECT r.allocations,r.allowed_employees,a.employee FROM unified_tasks r JOIN accounts a ON a.id=r.account WHERE r.state='active' AND r.not_before=0").fetchall():
            for node,cards in json.loads(row['allocations']).items():
                db.execute('INSERT OR REPLACE INTO unified_continuing VALUES (?,?,?)',(node,row['employee'],boundary))
                for employee in json.loads(row['allowed_employees']):
                    shared.setdefault((node,employee),set()).update(cards)
        for (node,employee),cards in shared.items():
            db.execute('INSERT OR REPLACE INTO unified_shared_continuing VALUES (?,?,?,?)', (node,employee,boundary,json.dumps(sorted(cards))))
        db.execute('DELETE FROM confirmations WHERE job IN (SELECT id FROM unified_tasks WHERE not_before=0)')
        db.execute('DELETE FROM reservations WHERE id IN (SELECT id FROM unified_tasks WHERE not_before=0)')
        db.execute('DELETE FROM unified_tasks WHERE not_before=0')
        # Unstarted reservations from a completed night must not be started anew at 09:00.
        expired=[row['id'] for row in db.execute("SELECT id,not_before,finish_before,night_fixed FROM unified_tasks WHERE not_before>0 AND state IN ('queued','ready')")
                 if not (self.strict_nights and row['night_fixed'])
                 and (row['finish_before'] or self.night_cutoff(row['not_before']-0.001))<=boundary]
        for job in expired:
            db.execute('DELETE FROM confirmations WHERE job=?',(job,))
            db.execute('DELETE FROM reservations WHERE id=?',(job,))
            db.execute('DELETE FROM unified_tasks WHERE id=?',(job,))
        db.execute("INSERT OR REPLACE INTO unified_meta VALUES ('reset_checkpoint',?)", (str(boundary),))
        db.execute("INSERT OR REPLACE INTO unified_meta VALUES ('last_daily_reset',?)", (json.dumps({'at':boundary,'count':count,'cancelled_nights':len(expired)}),))

    def continuing(self):
        with self.store.db() as db:
            rows=[dict(r) for r in db.execute('SELECT * FROM unified_continuing')]
            rows.extend({**dict(r), 'cards':json.loads(r['cards'])} for r in db.execute('SELECT * FROM unified_shared_continuing'))
            return rows

    def released_usage(self):
        with self.store.db() as db:
            return [{**dict(row),'allowed_employees':json.loads(row['allowed_employees']),
                     'allocations':json.loads(row['allocations'])}
                    for row in db.execute('SELECT * FROM unified_overtime_releases WHERE released_at>?',(time.time()-7*86400,))]

    def alerts(self, rows, user, now, reset, blocked, dependent, joint):
        result = []
        if reset and (reset['count'] or reset.get('cancelled_nights')) and 0 <= now-reset['at'] < 600:
            message=('上午 9 点已清空普通任务预约；夜间预约按实际排程执行，未确认的过期预约已取消。'
                     if self.normal_priority else '上午 9 点已清空普通任务预约；已确认的夜间预约按固定时段执行，未确认的过期预约已取消。'
                     if self.strict_nights else '上午 9 点已清空普通任务预约，取消未开始的夜间预约；已开始的夜间任务保留，实际任务未停止。')
            result.append({'id':'daily-reset:'+str(reset['at']), 'kind':'reset','message':message})
        if not user:
            return result
        for row in rows:
            if (self.normal_priority and row['not_before'] and row['state']=='queued' and row['night_schedule_blocked']
                    and (row['account']==user['id'] or user.get('employee','') in json.loads(row['allowed_employees']))):
                result.append({'id':'night-schedule-blocked:'+row['id'],'kind':'night-rescheduled',
                               'message':'夜间预约的前序资源尚未确认释放，开始时间待确认；请查看页面。'})
            if (self.normal_priority and row['not_before'] and row['state']=='queued' and row['night_rescheduled_at']
                    and not row['night_schedule_blocked']
                    and (row['account']==user['id'] or user.get('employee','') in json.loads(row['allowed_employees']))):
                start=datetime.fromtimestamp(row['not_before'],self.timezone).strftime('%m-%d %H:%M')
                result.append({'id':'night-rescheduled:'+row['id']+':'+str(row['night_rescheduled_at']),
                               'kind':'night-rescheduled','message':'普通任务优先，夜间预约已顺延至 '+start+'；请查看页面并调整任务启动时间。'})
            if self.overtime_control and row['state']=='active' and not row['not_before'] and row['extension_at']:
                occupied=set(json.loads(row['allocations']))
                waiting=next((r for r in rows if r['state']=='queued' and not r['not_before']
                              and (r['dual'] or not r['fixed_node'] or r['fixed_node'] in occupied)),None)
                if waiting and (waiting['account']==user['id'] or user.get('employee','') in json.loads(waiting['allowed_employees'])):
                    total=('至少 ' if not row['extension_total_complete'] else '')+str(row['extension_total_minutes'])
                    result.append({'id':'usage-extended:'+row['id']+':'+str(row['extension_at']),
                                   'kind':'extended-next','message':row['name']+'（'+row['employee']+'）累计延长使用 '+total+' 分钟；请查看更新后的预计开始时间。'})
            if row['state']!='active' or not row['activated'] or now < row['activated']+row['requested_seconds']:
                continue
            label=row['name']+'（'+row['employee']+'）'
            bottleneck=bool(blocked.get(row['id']))
            jointly_blocking=bool(joint.get(row['id'])) and not bottleneck
            indirectly_blocking=bool(dependent.get(row['id'])) and not bottleneck and not jointly_blocking
            if row['account']==user['id'] or user.get('employee','') in json.loads(row['allowed_employees']):
                interval=60 if self.overtime_control and not row['not_before'] else 300 if bottleneck or jointly_blocking or indirectly_blocking else 900
                status='blocking' if bottleneck else 'joint' if jointly_blocking else 'dependent' if indirectly_blocking else 'nonblocking'
                message=('当前正阻挡后续预约，请尽快自行停止任务并结束登记。' if bottleneck else
                         '与其他超时任务共同影响后续预约，请尽快自行停止任务并结束登记。' if jointly_blocking else
                         '占用等待中预约所需资源；其他资源也可能尚未释放，请尽快自行停止任务并结束登记。' if indirectly_blocking else
                         '目前不是可立即开始预约的瓶颈；用完请自行停止任务并结束登记。')
                if self.overtime_control and not row['not_before']:
                    message=('请在超时后的 10 分钟内结束登记或选择延长；普通任务最晚在凌晨 1 点释放登记。'
                             if self.normal_priority else '请在超时后的 10 分钟内结束登记，或选择延长 10、20、30 分钟。')
                result.append({'id':'usage-overdue:'+row['id']+':owner:'+status,
                               'job':row['id'],'repeat_seconds':interval,
                               'kind':'overdue' if bottleneck else 'overdue-joint' if jointly_blocking else 'overdue-dependent' if indirectly_blocking else 'overdue-nonblocking',
                               'message':label+'的预约已使用超时，'+message})
            if now-row['activated']-row['requested_seconds'] > 300:
                minutes=max(1,math.ceil((now-row['activated']-row['requested_seconds'])/60))
                for waiting in blocked.get(row['id'],[]):
                    if waiting['account']==user['id'] and user['id']!=row['account']:
                        result.append({'id':'usage-overdue:'+row['id']+':next:'+waiting['id'],'kind':'overdue-next',
                                       'message':label+'的预约已超时 '+str(minutes)+' 分钟；预计其释放资源后，你的预约可以立即开始。'})
                if not bottleneck:
                    for waiting in dependent.get(row['id'],[]):
                        if waiting['account']==user['id'] and user['id']!=row['account']:
                            shared=waiting in joint.get(row['id'],[])
                            result.append({'id':'usage-overdue:'+row['id']+':dependent-next:'+waiting['id'],
                                           'kind':'overdue-joint-next' if shared else 'overdue-dependent-next',
                                           'message':label+'的预约已超时 '+str(minutes)+' 分钟，'+('与其他超时任务共同影响你前方的资源安排' if shared else '可能影响你前方的资源安排，其他资源也可能尚未释放')+'；请按队列顺序等待，预计开始时间以页面为准。'})
        return result

    def _expire_offers(self, db, now):
        db.execute('DELETE FROM unified_notice_events WHERE at<?',(now-7*86400,))
        pending = [dict(row) for row in db.execute("SELECT * FROM unified_tasks WHERE state IN ('queued','ready') ORDER BY queue_order,rowid")]
        expired = [row for row in pending if row['state']=='ready' and
                   ((row['claim_before'] and row['claim_before']<=now) or
                    (self.strict_nights and not row['not_before'] and any(a<=now<b for a,b in self.avoid_windows(json.loads(row['avoid_slots']),now))) or
                    (not row['not_before'] and self.midnight_separation and self.normal_allowed_from(now)>now) or
                    (not row['not_before'] and not self.midnight_separation and now >= (self.normal_one_am(row['created']) if self.strict_nights else self.normal_two_am(row['created']))))]
        missed = {row['id'] for row in expired if row['ready_since'] and now>=row['ready_since']+600}
        removed = {row['id'] for row in expired if self.missed_claim_limit and row['id'] in missed and row['missed_claims']+1>=self.missed_claim_limit}
        for row in expired:
            if row['id'] in removed:
                owner=db.execute('SELECT employee,name FROM accounts WHERE id=?',(row['account'],)).fetchone()
                if owner:
                    db.execute('INSERT OR IGNORE INTO unified_notice_events VALUES (?,?,?,?,?,?,?,?)',
                        ('missed:'+row['id'], 'missed-cancel',row['id'],owner['employee'],owner['name'],
                         row['allowed_employees'],now,json.dumps({'limit':self.missed_claim_limit})))
                db.execute('DELETE FROM confirmations WHERE job=?',(row['id'],))
                db.execute('DELETE FROM reservations WHERE id=?',(row['id'],))
                db.execute('DELETE FROM unified_tasks WHERE id=?',(row['id'],))
                continue
            db.execute("UPDATE unified_tasks SET state='queued',allocations='{}',claim_before=0,ready_since=0,offer_id='',offer_end_before=0,backfill_end_before=0,missed_claims=missed_claims+? WHERE id=?",
                       (int(row['id'] in missed), row['id']))
        pending=[row for row in pending if row['id'] not in removed]
        missed-=removed
        if not missed:
            return
        # Move missed offers back within their own task type. Keep the slots
        # of other task types in place so expiry cannot reshuffle their priority.
        groups = {(night,urgent): [] for night in (False,True) for urgent in (False,True)}
        for row in pending:
            groups[(bool(row['not_before']),bool(row['urgent']))].append(row)
        for key, group in groups.items():
            order, i = [], 0
            while i < len(group):
                if group[i]['id'] not in missed:
                    order.append(group[i]); i += 1; continue
                start = i
                while i < len(group) and group[i]['id'] in missed:
                    i += 1
                if i < len(group):
                    order.append(group[i]); order.extend(group[start:i]); i += 1
                else:
                    order.extend(group[start:i])
            groups[key] = iter(order)
        for position, slot in enumerate(pending, 1):
            row = next(groups[(bool(slot['not_before']),bool(slot['urgent']))])
            db.execute('UPDATE unified_tasks SET queue_order=? WHERE id=?', (position, row['id']))

    def tick(self):
        with self.store.db() as db:
            self._schedule(db, time.time())

    def lock_due_night_takeovers(self,now):
        if not self.normal_priority:return
        with self.store.db() as db:
            self._schedule(db,now)
            db.execute('''UPDATE unified_tasks SET night_takeover=1 WHERE state='queued' AND night_fixed=1
                          AND night_schedule_blocked=0 AND not_before>0 AND not_before<=?''',(now,))

    def due_night_tasks(self,now):
        if not self.strict_nights:return []
        with self.store.db() as db:
            rows=[dict(row) for row in db.execute('''SELECT r.*,a.employee FROM unified_tasks r
                JOIN accounts a ON a.id=r.account WHERE r.not_before>0 AND r.not_before<=?
                AND (r.state='active' OR (r.state='queued' AND r.night_fixed=1 AND r.night_schedule_blocked=0
                AND (?=0 OR r.night_takeover=1)))''',(now,int(self.normal_priority)))]
        rows=[row for row in rows if self.night_hard_cutoff(row) is None or now<self.night_hard_cutoff(row)]
        for row in rows:
            row['allocations']=json.loads(row['allocations'])
            row['allowed_employees']=json.loads(row['allowed_employees'])
        return rows

    def finish_night_takeovers(self,snapshots,blocked_nodes,blocked_cards,now):
        """Commit only unchanged, freshly verified night handoffs."""
        if not self.strict_nights:return
        with self.store.db() as db:
            for expected in snapshots:
                row=db.execute('''SELECT r.*,a.employee FROM unified_tasks r JOIN accounts a ON a.id=r.account
                                  WHERE r.id=?''',(expected['id'],)).fetchone()
                if not row:continue
                row=dict(row);allocations=json.loads(row['allocations']);allowed=json.loads(row['allowed_employees'])
                if (row['state']!=expected['state'] or row['not_before']!=expected['not_before']
                        or row['requested_seconds']!=expected['requested_seconds']
                        or row['night_end_requested']!=expected['night_end_requested']
                        or row['night_takeover']!=expected['night_takeover']
                        or row['employee']!=expected['employee'] or allocations!=expected['allocations']
                        or allowed!=expected['allowed_employees']):continue
                deadline=self.night_hard_cutoff(row)
                if deadline is not None and now>=deadline:
                    self._schedule(db,now)
                    continue
                end=(row['activated'] or row['not_before'])+row['requested_seconds']
                if deadline is not None:end=min(end,deadline)
                if row['night_end_requested']:end=min(end,row['night_end_requested'])
                if now>=end:
                    db.execute('DELETE FROM unified_tasks WHERE id=?',(row['id'],))
                    db.execute('DELETE FROM confirmations WHERE job=?',(row['id'],))
                    db.execute('DELETE FROM reservations WHERE id=?',(row['id'],))
                    continue
                if any(node in blocked_nodes or set(cards)&blocked_cards.get(node,set())
                       for node,cards in allocations.items()):continue
                if row['state']=='queued' and row['night_fixed'] and now>=row['not_before']:
                    for normal in db.execute("SELECT id,allocations FROM unified_tasks WHERE not_before=0 AND state IN ('active','ready')").fetchall():
                        held=json.loads(normal['allocations']);remaining={}
                        for node,cards in held.items():
                            kept=[card for card in cards if card not in allocations.get(node,[])]
                            if kept:remaining[node]=kept
                        if remaining==held:continue
                        db.execute('''INSERT INTO unified_night_reclaims(job,night_job,at,old_allocations,new_allocations)
                                      VALUES (?,?,?,?,?)''',(normal['id'],row['id'],now,json.dumps(held),json.dumps(remaining)))
                        if remaining:
                            db.execute('UPDATE unified_tasks SET allocations=? WHERE id=?',(json.dumps(remaining),normal['id']))
                        else:
                            db.execute('DELETE FROM unified_tasks WHERE id=?',(normal['id'],))
                            db.execute('DELETE FROM confirmations WHERE job=?',(normal['id'],))
                            db.execute('DELETE FROM reservations WHERE id=?',(normal['id'],))
                    db.execute("UPDATE unified_tasks SET state='active',activated=? WHERE id=?",(row['not_before'],row['id']))

    def auto_finish_nights(self, monitor, now=None):
        if self.strict_nights:
            # Strict nights own their slot until the booked end or explicit end.
            # A temporarily idle process inventory is not an early release.
            return []
        now = time.time() if now is None else now
        with self.store.db() as db:
            rows = [dict(row) for row in db.execute('''SELECT r.*,a.employee FROM unified_tasks r
                JOIN accounts a ON a.id=r.account WHERE r.state='active' AND r.not_before>0''')
                    if now < (row['finish_before'] or self.night_cutoff(row['not_before']-0.001))]
            for row in rows:
                row['allocations']=json.loads(row['allocations'])
                row['allowed_employees']=json.loads(row['allowed_employees'])
            observed, finished = monitor.night_completion_decisions(rows, now)
            for job in observed:
                db.execute("UPDATE unified_tasks SET night_seen_usage=1 WHERE id=? AND state='active' AND not_before>0", (job,))
            for job in finished:
                db.execute("DELETE FROM unified_tasks WHERE id=? AND state='active' AND not_before>0", (job,))
                db.execute('DELETE FROM confirmations WHERE job=?', (job,))
                db.execute('DELETE FROM reservations WHERE id=?', (job,))
            if finished:
                self._schedule(db, now)
            return finished

    def _planning_inputs(self, rows, excluded=None, now=None):
        now = time.time() if now is None else now
        busy, waiting = [], []
        for row in rows:
            if row['id'] == excluded:
                continue
            if self.strict_nights and row['state']=='queued' and row['not_before'] and not row.get('night_fixed'):
                # A legacy night that cannot be fixed is kept for review, not
                # silently started later than its requested time.
                continue
            if self.strict_nights and row['state']=='queued' and row.get('night_fixed'):
                if self.normal_priority and not row.get('night_takeover'):
                    # Unstarted nights move behind normal reservations.
                    continue
                end=row['not_before']+row['requested_seconds']
                buffered=(end if self.midnight_separation else end+self.buffer) if end>now else None
                for node,cards in json.loads(row['allocations']).items():
                    busy.append({'node':node,'cards':cards,'start':row['not_before'],
                                 'end':buffered,'direct_end':end if end>now else None,'fixed_night':True})
                continue
            if row['state'] in ('ready', 'active'):
                end = (row['activated'] + row['requested_seconds'] + self.buffer
                       if row['state'] == 'active' and row['activated'] and row['requested_seconds'] else
                       row['claim_before'] + row['requested_seconds'] + self.buffer
                       if row['state'] == 'ready' and row['claim_before'] else None)
                if row['state']=='ready' and row.get('backfill_end_before'):
                    end=row['backfill_end_before']+self.buffer
                if row['state'] == 'active' and row['activated'] and row['requested_seconds'] and now >= row['activated'] + row['requested_seconds']:
                    if self.overtime_control and not row['not_before'] and now<row['activated']+row['requested_seconds']+600:
                        end=row['activated']+row['requested_seconds']+600
                        direct_end=end
                    else:end=direct_end=None
                else:direct_end = end if end is None else end-self.buffer
                for node, cards in json.loads(row['allocations']).items():
                    busy.append({'node': node, 'cards': cards, 'start': row['activated'] or row['created'], 'end': end, 'direct_end': direct_end})
            else:
                item = {'id': row['id'], 'cards': row['cards'], 'dual': bool(row['dual']),
                        'seconds': row['requested_seconds'] or 3600, 'not_before': row['not_before'],
                        'night': bool(row['not_before']),
                        'finish_before': row['finish_before'] if not row['not_before'] else 0,
                        'start_before': (self.normal_one_am(row['created']) if self.strict_nights else self.normal_two_am(row['created'])) if not row['not_before'] else 0}
                if self.midnight_separation and not row['not_before']:
                    allowed=self.normal_allowed_from(now)
                    item.update(not_before=allowed,finish_before=0,start_before=self.normal_night_end(allowed),midnight_separation=True)
                if self.strict_nights and not row['not_before']:
                    item['clip_finish_before']=self.normal_night_end(self.normal_allowed_from(now) if self.midnight_separation else row['created'])
                    item['avoid_windows']=self.avoid_windows(json.loads(row.get('avoid_slots','[]')),now)
                    item['backfill_min_seconds']=row.get('backfill_min_seconds',0)
                if row['fixed_node']:
                    item['allowed_nodes'] = [row['fixed_node']]
                waiting.append(item)
        return busy, waiting

    def _adopt_legacy_nights(self,db,now):
        if not self.strict_nights:return
        from queue_night_policy import reserve_night
        rows=[dict(row) for row in db.execute("SELECT * FROM unified_tasks WHERE not_before>0 AND state IN ('active','ready','queued') ORDER BY queue_order,rowid")]
        reservations=[]
        for row in rows:
            allocations=json.loads(row['allocations'])
            if allocations and (row['state'] in ('active','ready') or row['night_fixed']):
                begin=row['activated'] if row['state']=='active' and not row['night_fixed'] else row['not_before']
                reservations.append({'start':begin,'end':begin+row['requested_seconds'],'allocations':allocations})
        for row in rows:
            if row['state']!='queued' or row['night_fixed'] or row['not_before']<=now:continue
            allocation=reserve_night(self.nodes,reservations,row['not_before'],row['requested_seconds'],
                                     row['cards'],bool(row['dual']),row['fixed_node'])
            if allocation is None:continue
            db.execute('UPDATE unified_tasks SET night_fixed=1,allocations=? WHERE id=?',(json.dumps(allocation),row['id']))
            reservations.append({'start':row['not_before'],'end':row['not_before']+row['requested_seconds'],'allocations':allocation})

    def _reschedule_nights(self,db,rows,predictions,now):
        from queue_night_replan import reschedule
        blockers=[];nights=[]
        for row in rows:
            allocations=json.loads(row['allocations'])
            if row['not_before']:
                if row['state']=='active' or row.get('night_takeover'):
                    start=row['activated'] or row['not_before']
                    end=start+row['requested_seconds']
                    if row.get('night_end_requested'):end=min(end,row['night_end_requested'])
                    blockers.append({'start':start,'end':end if end>now else None,'allocations':allocations})
                elif row['state']=='queued' and row['night_fixed']:
                    night={'id':row['id'],'requested':row['night_requested_start'] or row['not_before'],
                                   'scheduled':row['not_before'],'seconds':row['requested_seconds'],
                                   'order':row['queue_order'],'allocations':allocations}
                    hard_end=self.night_hard_cutoff(row)
                    if hard_end is not None:night.update(latest_end=hard_end,minimum_seconds=60)
                    nights.append(night)
                continue
            if row['state']=='active' and row['activated']:
                start=row['activated'];end=min(start+row['requested_seconds']+600,self.normal_night_end(start))
            elif row['state']=='ready':
                start=now;end=min(row['claim_before']+row['requested_seconds']+600,self.normal_night_end(row['created']))
            else:
                prediction=predictions.get(row['id'],{})
                start=prediction.get('start')
                if start is None:continue
                allocations=prediction.get('allocations',{})
                # Include the future claim window and overrun grace, bounded by 01:00.
                end=min(start+prediction.get('seconds',row['requested_seconds'])+1200,self.normal_night_end(row['created']))
            if allocations and end>start:
                blockers.append({'start':start,'end':end,'allocations':allocations})
        planned=reschedule(nights,blockers)
        for row in rows:
            if row['id'] not in planned:continue
            start=planned[row['id']]
            if start is None:
                db.execute('UPDATE unified_tasks SET night_schedule_blocked=1 WHERE id=?',(row['id'],))
                row['night_schedule_blocked']=1
            else:
                changed=start>row['not_before']
                hard_end=self.night_hard_cutoff(row)
                seconds=min(row['requested_seconds'],math.floor(hard_end-start)) if hard_end is not None else row['requested_seconds']
                db.execute('UPDATE unified_tasks SET not_before=?,requested_seconds=?,night_schedule_blocked=0,night_rescheduled_at=CASE WHEN ? THEN ? ELSE night_rescheduled_at END WHERE id=?',
                           (start,seconds,int(changed),now,row['id']))
                row['not_before']=start;row['requested_seconds']=seconds;row['night_schedule_blocked']=0
                if changed:row['night_rescheduled_at']=now

    def _schedule(self, db, now):
        self._reset_if_due(db, now)
        if self.night_carryover_cutoff_hour is not None:
            nights=[dict(row) for row in db.execute('''SELECT r.*,a.employee FROM unified_tasks r JOIN accounts a ON a.id=r.account
                WHERE r.not_before>0 AND r.state IN ('queued','ready','active')''')]
            for row in nights:
                deadline=self.night_hard_cutoff(row)
                if now>=deadline:
                    if row['state']=='active':
                        db.execute('''INSERT OR IGNORE INTO unified_overtime_releases(job,account,employee,allowed_employees,released_at,reason,allocations)
                                      VALUES (?,?,?,?,?,?,?)''', (row['id'],row['account'],row['employee'],row['allowed_employees'],now,'night-cutoff',row['allocations']))
                    db.execute('DELETE FROM confirmations WHERE job=?',(row['id'],))
                    db.execute('DELETE FROM reservations WHERE id=?',(row['id'],))
                    db.execute('DELETE FROM unified_tasks WHERE id=?',(row['id'],))
                elif row['state']=='active' and row['activated'] and row['activated']+row['requested_seconds']>deadline:
                    db.execute('UPDATE unified_tasks SET requested_seconds=? WHERE id=?',
                               (max(1,math.floor(deadline-row['activated'])),row['id']))
        if self.normal_priority or self.midnight_separation:
            for row in db.execute("SELECT id,activated,requested_seconds FROM unified_tasks WHERE state='active' AND not_before=0 AND activated>0").fetchall():
                limit=self.normal_night_end(row['activated'])
                if row['activated']+row['requested_seconds']>limit:
                    db.execute('UPDATE unified_tasks SET requested_seconds=? WHERE id=?',
                               (max(1,math.floor(limit-row['activated'])),row['id']))
        if self.overtime_control:
            active=db.execute('''SELECT r.id,r.account,r.allowed_employees,r.activated,r.requested_seconds,r.allocations,a.employee FROM unified_tasks r
                JOIN accounts a ON a.id=r.account WHERE r.state='active' AND r.not_before=0
                AND r.activated>0 AND r.requested_seconds>0''').fetchall()
            expired=[row for row in active if now>=min(row['activated']+row['requested_seconds']+600,
                      self.normal_night_end(row['activated']) if (self.normal_priority or self.midnight_separation) else math.inf)]
            for row in expired:
                reason=('cutoff' if (self.normal_priority or self.midnight_separation) and now>=self.normal_night_end(row['activated'])
                        and self.normal_night_end(row['activated'])<row['activated']+row['requested_seconds']+600 else 'overtime')
                db.execute('INSERT OR IGNORE INTO unified_overtime_releases(job,account,employee,allowed_employees,released_at,reason,allocations) VALUES (?,?,?,?,?,?,?)',
                           (row['id'],row['account'],row['employee'],row['allowed_employees'],now,reason,row['allocations']))
                db.execute('DELETE FROM confirmations WHERE job=?',(row['id'],))
                db.execute('DELETE FROM reservations WHERE id=?',(row['id'],))
                db.execute('DELETE FROM unified_tasks WHERE id=?',(row['id'],))
            db.execute('DELETE FROM unified_overtime_releases WHERE released_at<?',(now-7*86400,))
        # Upgrade previously offered night reservations without dropping their allocations.
        if self.strict_nights:
            db.execute("UPDATE unified_tasks SET state='queued',night_fixed=1,activated=0,claim_before=0,ready_since=0,offer_id='' WHERE state='ready' AND not_before>0 AND allocations!='{}'")
            db.execute("UPDATE unified_tasks SET state='queued',claim_before=0,ready_since=0,offer_id='' WHERE state='ready' AND not_before>0 AND allocations='{}'")
        else:
            db.execute("UPDATE unified_tasks SET state='active',activated=CASE WHEN ready_since>0 AND ready_since<=? THEN ready_since ELSE ? END,claim_before=0,ready_since=0,offer_id='' WHERE state='ready' AND not_before>0", (now,now))
        self._expire_offers(db, now)
        self._adopt_legacy_nights(db,now)
        if self.strict_nights:
            nights=db.execute("SELECT * FROM unified_tasks WHERE not_before>0 AND night_fixed=1 AND state IN ('queued','ready','active')").fetchall()
            for row in db.execute("SELECT * FROM unified_tasks WHERE state='active' AND not_before=0 AND activated>0").fetchall():
                end=self.normal_night_end(row['activated'])
                allocations=json.loads(row['allocations'])
                for night in nights:
                    if self.normal_priority:continue
                    if night['not_before']<=now:continue
                    reserved=json.loads(night['allocations'])
                    if any(set(cards).intersection(reserved.get(node,[])) for node,cards in allocations.items()):
                        end=min(end,night['not_before']-(0 if self.midnight_separation else self.buffer))
                if row['activated'] < self.normal_one_am(row['activated']) and row['activated']+row['requested_seconds'] > end:
                    db.execute('UPDATE unified_tasks SET requested_seconds=? WHERE id=?', (max(1,math.floor(end-row['activated'])),row['id']))
            for row in db.execute("SELECT * FROM unified_tasks WHERE state='ready' AND not_before=0").fetchall():
                allocations=json.loads(row['allocations'])
                end=self.normal_night_end(now if self.midnight_separation else row['created'])
                if row['backfill_end_before']:end=min(end,row['backfill_end_before'])
                for night in nights:
                    if self.normal_priority:continue
                    if night['not_before']<=now:continue
                    reserved=json.loads(night['allocations'])
                    if any(set(cards).intersection(reserved.get(node,[])) for node,cards in allocations.items()):
                        end=min(end,night['not_before']-(0 if self.midnight_separation else self.buffer))
                future_avoid=[begin for begin,finish in self.avoid_windows(json.loads(row['avoid_slots']),now) if begin>now]
                minimum=row['backfill_min_seconds'] if row['backfill_end_before'] else 60
                deadline=min([row['claim_before'],self.normal_one_am(now if self.midnight_separation else row['created']),end-minimum+0.001,*future_avoid])
                if deadline<=now:
                    db.execute("UPDATE unified_tasks SET state='queued',allocations='{}',claim_before=0,ready_since=0,offer_id='',offer_end_before=0,backfill_end_before=0 WHERE id=?",(row['id'],))
                else:
                    db.execute('UPDATE unified_tasks SET claim_before=?,offer_end_before=? WHERE id=?',(deadline,end,row['id']))
        rows = [dict(row) for row in db.execute("SELECT * FROM unified_tasks WHERE state IN ('ready','active','queued') ORDER BY queue_order,rowid")]
        # A due night reservation can take over expired night allocations without
        # consulting process inventory. Morning carryovers still require manual release.
        expired={row['id']:row for row in rows if not self.strict_nights and row['state']=='active' and row['not_before']
                 and row['activated'] and row['requested_seconds'] and now>=row['activated']+row['requested_seconds']
                 and now<(row['finish_before'] or self.night_cutoff(row['not_before']-0.001))}
        while expired:
            busy,waiting=self._planning_inputs(rows,now=now)
            baseline={p['id']:p for p in plan(self.nodes,busy,waiting,now,self.buffer)}
            reduced=[row for row in rows if row['id'] not in expired]
            busy,waiting=self._planning_inputs(reduced,now=now)
            candidate=plan(self.nodes,busy,waiting,now,self.buffer)
            queued={row['id']:row for row in rows if row['state']=='queued' and row['not_before']}
            release=set()
            for prediction in candidate:
                job=prediction['id'];actual=baseline.get(job,{})
                if job not in queued or prediction['start'] is None or prediction['start']>now:
                    continue
                if actual.get('start') is not None and actual['start']<=now:
                    continue
                for job_id,row in expired.items():
                    allocations=json.loads(row['allocations'])
                    if any(set(cards).intersection(allocations.get(node,[])) for node,cards in prediction['allocations'].items()):
                        release.add(job_id)
                if release:break
            if not release:
                # If a normal reservation wins the freed cards at the next
                # night's requested time, release the expired night for it first.
                normal_ids={row['id'] for row in rows if row['state']=='queued' and not row['not_before']}
                due_normal=[p for p in candidate if p['id'] in normal_ids and p['start'] is not None and p['start']<=now]
                due_night=[p for p in candidate if p['id'] in queued and queued[p['id']]['not_before']<=now and p['start'] is not None]
                for job_id,row in expired.items():
                    allocations=json.loads(row['allocations'])
                    def overlaps(prediction):
                        return any(set(cards).intersection(allocations.get(node,[])) for node,cards in prediction['allocations'].items())
                    if any(overlaps(p) for p in due_normal) and any(overlaps(p) for p in due_night):release.add(job_id)
            if not release:break
            for job in release:
                db.execute('DELETE FROM unified_tasks WHERE id=?',(job,))
                db.execute('DELETE FROM confirmations WHERE job=?',(job,))
                db.execute('DELETE FROM reservations WHERE id=?',(job,))
                expired.pop(job,None)
            rows=[row for row in rows if row['id'] not in release]
        busy, waiting = self._planning_inputs(rows,now=now)
        predictions = {item['id']: item for item in plan(self.nodes, busy, waiting, now, self.buffer)}
        if self.normal_priority:
            self._reschedule_nights(db,rows,predictions,now)
        if self.strict_nights:
            for row in rows:
                if row['state']=='queued' and row.get('night_fixed'):
                    predictions[row['id']]={'id':row['id'],'start':None if row.get('night_schedule_blocked') else row['not_before'],
                                            'allocations':json.loads(row['allocations']),
                                            'seconds':row['requested_seconds']}
        previous=[]
        for row in rows:
            prediction = predictions.get(row['id'])
            if self.strict_nights and row.get('night_fixed'):
                if prediction:previous.append(prediction)
                continue
            if prediction and prediction['start'] is not None and prediction['start'] <= now:
                if row['not_before']:
                    db.execute("UPDATE unified_tasks SET state='active',activated=?,allocations=?,claim_before=0,ready_since=0,offer_id='' WHERE id=? AND state='queued'",
                               (now, json.dumps(prediction['allocations']), row['id']))
                    previous.append(prediction)
                    continue
                deadlines=[now+600]
                if not row['not_before']:
                    deadlines.append(self.normal_one_am(now if self.midnight_separation else row['created']) if self.strict_nights else self.normal_two_am(row['created']))
                if row['finish_before'] and not row['not_before'] and not self.midnight_separation:
                    deadlines.append(row['finish_before'] - row['requested_seconds'])
                offer_end=0
                if self.strict_nights and not row['not_before']:
                    offer_end=self.normal_night_end(now if self.midnight_separation else row['created'])
                    backfill_end=prediction.get('backfill_end_before',0)
                    if backfill_end:offer_end=min(offer_end,backfill_end)
                    deadlines.extend(begin for begin,end in self.avoid_windows(json.loads(row['avoid_slots']),now) if begin>now)
                    fixed_night_ids={r['id'] for r in rows if r['not_before'] and r.get('night_fixed')}
                    for other in predictions.values():
                        if self.normal_priority:continue
                        if other['id'] not in fixed_night_ids:continue
                        if other['id']==row['id'] or other['start'] is None or other['start']<=now:continue
                        if any(set(cards).intersection(other['allocations'].get(node,[]))
                               for node,cards in prediction['allocations'].items()):
                            offer_end=min(offer_end,other['start']-(0 if self.midnight_separation else self.buffer))
                    deadlines.append(offer_end-(row['backfill_min_seconds'] if backfill_end else 60)+0.001)
                for earlier in previous:
                    if self.strict_nights and earlier['id'] in fixed_night_ids:
                        continue
                    if earlier['start'] is None or earlier['start'] <= now:
                        continue
                    for node,cards in prediction['allocations'].items():
                        if set(cards).intersection(earlier['allocations'].get(node,[])):
                            if not prediction.get('backfill_end_before'):
                                deadlines.append(earlier['start'] - prediction.get('seconds',row['requested_seconds'] or 3600) - self.buffer)
                claim_before=min(deadlines)
                db.execute("UPDATE unified_tasks SET state='ready',allocations=?,claim_before=?,ready_since=?,offer_id=?,offer_end_before=?,backfill_end_before=? WHERE id=? AND state='queued'",
                           (json.dumps(prediction['allocations']), claim_before, now, secrets.token_hex(16),offer_end,prediction.get('backfill_end_before',0),row['id']))
            if prediction:
                previous.append(prediction)
        if expired:
            # Show the future night-to-night handoff even while the prior
            # reservation remains active until the successor is due.
            reduced=[row for row in rows if row['id'] not in expired]
            future_busy,future_waiting=self._planning_inputs(reduced,now=now)
            future=plan(self.nodes,future_busy,future_waiting,now,self.buffer)
            night_ids={row['id'] for row in rows if row['state']=='queued' and row['not_before']}
            for prediction in future:
                current=predictions.get(prediction['id'],{})
                if (prediction['id'] in night_ids and prediction['start'] is not None and prediction['start']>now
                        and current.get('start') is None):
                    predictions[prediction['id']]=prediction
        return predictions

    def bottlenecks(self, rows, now):
        queued = {row['id']: row for row in rows if row['state']=='queued'}
        if not queued:
            return {}
        result = {}
        busy, waiting = self._planning_inputs(rows,now=now)
        baseline = {p['id']: p for p in plan(self.nodes, busy, waiting, now, self.buffer)}
        immediate = {job for job, prediction in baseline.items()
                     if prediction['start'] is not None and prediction['start'] <= now}
        for row in rows:
            if row['state'] != 'active' or not row['activated']:
                continue
            released_busy, waiting = self._planning_inputs(rows, row['id'],now=now)
            released = plan(self.nodes, released_busy, waiting, now, self.buffer)
            finish = row['activated'] + row['requested_seconds'] if row['requested_seconds'] else 0
            # Immediate release is meaningful for an already overdue task.
            # For work still within its reservation, mark only the release
            # that actually determines a planned waiting start below; otherwise
            # every job whose early release enables a backfill is mislabeled.
            enabled = ([queued[p['id']] for p in released
                        if p['id'] not in immediate and p['start'] is not None and p['start'] <= now]
                       if finish <= now else [])
            # When multiple tasks occupy a waiter's cards, the last estimated
            # release is a bottleneck even though no single release helps now.
            if finish > now:
                allocations = json.loads(row['allocations'])
                held = released_busy + [
                    {'node': node, 'cards': cards, 'start': row['activated'], 'end': None, 'direct_end': None}
                    for node, cards in allocations.items() if cards]
                held_predictions = {p['id']: p for p in plan(self.nodes, held, waiting, now, self.buffer)}
                for job_id, prediction in baseline.items():
                    job = queued.get(job_id)
                    start = prediction['start']
                    if not job or (job['not_before'] and job['not_before'] > now) or start is None or start <= now:
                        continue
                    effective = finish if job['not_before'] else finish + self.buffer
                    if abs(start - effective) > 0.001:
                        continue
                    if not any(set(cards).intersection(allocations.get(node, []))
                               for node, cards in prediction['allocations'].items()):
                        continue
                    held_start = held_predictions.get(job_id, {}).get('start')
                    if held_start is None or held_start > start:
                        if job not in enabled:
                            enabled.append(job)
            if enabled:
                result[row['id']] = enabled
        return result

    def overdue_resource_dependencies(self, rows, now):
        """Find overdue allocations necessary for due waiters, including ties.

        Compare release of all overdue tasks with holding each one. Keep other
        active tasks and already-offered allocations in both simulations.
        """
        overdue=[row for row in rows if row['state']=='active' and row['activated']
                 and row['requested_seconds'] and now>=row['activated']+row['requested_seconds']]
        if not overdue:
            return {}, {}
        ids={row['id'] for row in overdue}
        reduced=[row for row in rows if row['id'] not in ids]
        busy,waiting=self._planning_inputs(reduced,now=now)
        queued={row['id']:row for row in reduced if row['state']=='queued'}
        baseline={p['id']:p for p in plan(self.nodes,busy,waiting,now,self.buffer)}
        dependencies={}
        for row in overdue:
            held,_=self._planning_inputs([row],now=now)
            predictions={p['id']:p for p in plan(self.nodes,busy+held,waiting,now,self.buffer)}
            for job_id,prediction in baseline.items():
                job=queued.get(job_id);start=prediction['start']
                if not job or (job['not_before'] and job['not_before']>now) or start is None:
                    continue
                allocations=json.loads(row['allocations'])
                if not any(set(cards).intersection(allocations.get(node, []))
                           for node,cards in prediction['allocations'].items()):
                    continue
                held_start=predictions.get(job_id,{}).get('start')
                if held_start is None or held_start>start:
                    dependencies.setdefault(job_id,[]).append(row['id'])
        # Alert the nearest affected waiter(s), not every later job whose
        # hypothetical plan also shifts because the entire queue shifts.
        candidates = {}
        for job_id, blockers in dependencies.items():
            for blocker in blockers:
                candidates.setdefault(blocker, []).append(job_id)
        dependent, joint = {}, {}
        for blocker, jobs in candidates.items():
            first = min(baseline[job_id]['start'] for job_id in jobs)
            for job_id in jobs:
                if abs(baseline[job_id]['start'] - first) > 0.001:
                    continue
                dependent.setdefault(blocker, []).append(queued[job_id])
                if len(dependencies[job_id]) > 1:
                    joint.setdefault(blocker, []).append(queued[job_id])
        return dependent, joint

    def overtime_allowances(self, rows, now, blocked):
        """Estimate remaining overtime without delaying the current queue plan.

        Compare the queue with this allocation released versus held indefinitely.
        Include both competing active allocations and earlier planned jobs.
        Preserve the buffer before a normal waiting job; night jobs use the
        scheduler's direct-release rule. All results remain estimates.
        """
        queued={row['id']:row for row in rows if row['state']=='queued'}
        if not queued:return {}
        active=[row for row in rows if row['state']=='active' and row['activated'] and row['requested_seconds']]
        baseline_busy,baseline_waiting=self._planning_inputs(rows,now=now)
        baseline=plan(self.nodes,baseline_busy,baseline_waiting,now,self.buffer)
        result={}
        for row in active:
            if row['id'] in blocked:continue
            busy,waiting=self._planning_inputs(rows,row['id'],now=now)
            released=plan(self.nodes,busy,waiting,now,self.buffer)
            held=busy+[
                {'node':node,'cards':cards,'start':row['activated'],'end':None,'direct_end':None}
                for node,cards in json.loads(row['allocations']).items() if cards]
            held_predictions={p['id']:p for p in plan(self.nodes,held,waiting,now,self.buffer)}
            references=[]
            for prediction in released:
                job=queued.get(prediction['id']);start=prediction['start']
                if not job or start is None:continue
                held_start=held_predictions.get(prediction['id'],{}).get('start')
                if held_start is not None and held_start<=start:continue
                for other in active:
                    if other['id']==row['id']:continue
                    finish=other['activated']+other['requested_seconds']
                    if finish<=now:continue
                    effective=finish if job['not_before'] else finish+self.buffer
                    if abs(start-effective)>0.001:continue
                    allocations=json.loads(other['allocations'])
                    if any(set(cards).intersection(allocations.get(node,[])) for node,cards in prediction['allocations'].items()):
                        references.append(finish)
            # The relevant release may belong to an earlier queued job (for
            # example, a single-node job before a dual-node waiter).  The
            # current plan's card assignment provides a conservative deadline.
            allocations=json.loads(row['allocations'])
            unknown=row['activated']+row['requested_seconds']<=now or any(p['start'] is None and p['id'] in queued
                        and (not queued[p['id']]['not_before'] or queued[p['id']]['not_before']<=now)
                        and (queued[p['id']]['dual'] or not queued[p['id']]['fixed_node']
                             or queued[p['id']]['fixed_node'] in allocations)
                        for p in baseline)
            for prediction in ([] if unknown else baseline):
                job=queued.get(prediction['id']);start=prediction['start']
                if not job or start is None or start<=now:
                    continue
                if not any(set(cards).intersection(allocations.get(node,[]))
                           for node,cards in prediction['allocations'].items()):
                    continue
                held_start=held_predictions.get(prediction['id'],{}).get('start')
                if held_start is not None and held_start<=start:
                    continue
                references.append(start-(0 if job['not_before'] else self.buffer))
            if references:
                result[row['id']]=max(0,min(references)-max(now,row['activated']+row['requested_seconds']))
        return result

    def view(self, user, notification_context=None):
        now = time.time()
        account = user['id'] if user else ''
        with self.store.db() as db:
            predictions = self._schedule(db, now)
            rows = [dict(row) for row in db.execute('''SELECT r.*,a.employee,a.name FROM unified_tasks r
                JOIN accounts a ON a.id=r.account WHERE r.state IN ('queued','ready','active')
                ORDER BY r.queue_order,r.rowid''')]
            reset_row=db.execute("SELECT value FROM unified_meta WHERE key='last_daily_reset'").fetchone()
            reset=json.loads(reset_row[0]) if reset_row else None
            reclaimed={row['job'] for row in db.execute("SELECT DISTINCT job FROM unified_night_reclaims WHERE job IN (SELECT id FROM unified_tasks WHERE state IN ('active','ready','queued'))")}
            releases=[dict(row) for row in db.execute('SELECT * FROM unified_overtime_releases WHERE released_at>?',(now-86400,))] if self.overtime_control and user else []
            missed_cancellations=[dict(row) for row in db.execute("SELECT * FROM unified_notice_events WHERE kind='missed-cancel' AND at>?",(now-86400,))] if user else []
            can_urgent=bool(user and user.get('employee') and db.execute('SELECT 1 FROM unified_urgent_users WHERE employee=?',(user['employee'],)).fetchone())
        blocked=self.bottlenecks(rows,now)
        dependent,joint=self.overdue_resource_dependencies(rows,now)
        allowances=self.overtime_allowances(rows,now,{**blocked,**dependent})
        alerts=self.alerts(rows,user,now,reset,blocked,dependent,joint)
        for cancellation in missed_cancellations:
            if user.get('employee','') in [cancellation['employee'],*json.loads(cancellation['allowed_employees'])]:
                alerts.append({'id':cancellation['id'],'kind':'missed-cancel',
                    'message':'预约多次分配后超过 10 分钟未登记开始，已自动取消；如仍需使用，请重新预约。'})
        if notification_context is not None:
            # A private exporter reuses the same calculation for every recipient.
            # Copy before the public view removes account IDs and decodes fields.
            import copy
            notification_context.update(copy.deepcopy(dict(rows=rows,now=now,reset=reset,
                blocked=blocked,dependent=dependent,joint=joint)))
        for release in releases:
            if release['account']==account or user.get('employee','') in json.loads(release['allowed_employees']):
                prefix=('夜间预约已到上午 '+f'{int(self.night_carryover_cutoff_hour):02d}:{int(self.night_carryover_cutoff_hour*60)%60:02d} 截止时间' if release['reason']=='night-cutoff'
                        else '已到凌晨 1 点截止时间' if release['reason']=='cutoff' else '预约超时超过 10 分钟')
                suffix=('登记释放本身不会停止容器；如果预约卡进入夜间接管，将按夜间规则清理。'
                        if self.normal_priority else '容器和实际任务不会自动停止，请自行检查并停止任务。')
                alerts.append({'id':'overtime-release:'+release['job'],'kind':'overtime-release',
                               'message':prefix+'，登记已自动释放；'+suffix})
        position = 0
        for row in rows:
            row['mine'] = row.pop('account') == account
            row['night_reclaimed']=row['id'] in reclaimed
            row.pop('title', None)
            row['allocations'] = json.loads(row['allocations'])
            row['allowed_employees'] = json.loads(row['allowed_employees'])
            row['collaborator'] = bool(user and user.get('employee') in row['allowed_employees'])
            row['avoid_slots'] = json.loads(row['avoid_slots'])
            if row['state'] in ('queued', 'ready'):
                position += 1
                row['position'] = position
            else:
                row['position'] = None
            row['can_claim'] = (row['mine'] or row['collaborator']) and row['state'] == 'ready'
            row['overdue_seconds'] = max(0,now-row['activated']-row['requested_seconds']) if row['state']=='active' and row['activated'] else 0
            row['overdue'] = bool(row['state']=='active' and row['activated'] and now>=row['activated']+row['requested_seconds'])
            row['overtime_release_at'] = (min(row['activated']+row['requested_seconds']+600,
                self.normal_night_end(row['activated']) if (self.normal_priority or self.midnight_separation) else math.inf)
                if self.overtime_control and not row['not_before'] and row['state']=='active' and row['activated'] else 0)
            row['bottleneck'] = bool(row['state']=='active' and row['id'] in blocked)
            row['overdue_bottleneck'] = bool(row['overdue'] and row['bottleneck'])
            row['overdue_joint_blocker'] = bool(row['overdue'] and row['id'] in joint and not row['bottleneck'])
            row['overdue_pending_blocker'] = bool(row['overdue'] and row['id'] in dependent and not row['bottleneck'] and not row['overdue_joint_blocker'])
            row['overtime_remaining_seconds'] = allowances.get(row['id'])
            row['claim_remaining_seconds'] = max(0, row['claim_before']-now) if row['state']=='ready' else 0
            prediction = predictions.get(row['id'], {})
            row['backfill_planned']=bool(row['backfill_end_before'] or prediction.get('backfill_end_before'))
            row['estimated_start'] = prediction.get('start') if row['state'] == 'queued' else None
            row['window_expired'] = bool(not self.midnight_separation and not row['not_before'] and row['finish_before'] and now + row['requested_seconds'] > row['finish_before'] and row['state'] != 'active')
            estimated = row['activated'] if row['state']=='active' else now if row['state']=='ready' else prediction.get('start')
            row['effective_requested_seconds']=row['requested_seconds']
            if self.strict_nights and not row['not_before']:
                if row['state']=='ready':
                    row['effective_requested_seconds']=min(row['requested_seconds'],max(0,math.floor(self.normal_night_end(now if self.midnight_separation else row['created'])-now)))
                    if row['offer_end_before']:
                        row['effective_requested_seconds']=min(row['effective_requested_seconds'],max(0,math.floor(row['offer_end_before']-now)))
                elif row['state']=='queued' and prediction.get('seconds') is not None:
                    row['effective_requested_seconds']=prediction['seconds']
            two_am = self.normal_two_am(row['created']) if not row['not_before'] else 0
            row['normal_start_after_two'] = bool(not self.midnight_separation and row['mine'] and two_am and row['state'] == 'queued' and estimated is None)
            row['normal_start_after_one'] = bool(self.strict_nights and row['normal_start_after_two'])
            row['normal_start_after_midnight'] = bool(self.normal_priority and row['normal_start_after_two'])
            row['normal_start_unknown'] = False
            row['night_unconfirmed']=bool(self.strict_nights and row['state']=='queued' and row['not_before'] and not row['night_fixed'])
            row['night_end_overdue'] = bool(not self.midnight_separation and row['not_before'] and row['finish_before'] and (now > row['finish_before'] or (estimated is not None and estimated + row['requested_seconds'] > row['finish_before'])))
            row['morning_carryover'] = bool(row['state']=='active' and row['not_before'] and now >= (row['finish_before'] or self.night_cutoff(row['not_before']-0.001)))
            row['planned_allocations'] = prediction.get('allocations', {}) if row['state'] == 'queued' else {}
            row['node'] = ', '.join(row['allocations']) or row['fixed_node'] or '待分配'
            row['machine'] = 'dual' if row['dual'] else row['fixed_node'] or 'auto'
            row['total_cards'] = row['cards'] * (2 if row['dual'] else 1)
        for row in rows:
            waiting=[r for r in rows if r['state']=='queued' and bool(r['not_before'])==bool(row['not_before']) and r['urgent']==row['urgent']]
            pending=[r for r in rows if r['state'] in ('queued','ready') and bool(r['not_before'])==bool(row['not_before']) and r['urgent']==row['urgent']]
            row['can_raise_priority']=bool(user and user.get('admin') and not row['urgent'] and row in waiting and waiting.index(row)>0)
            row['can_lower_priority']=bool(user and not row['urgent'] and (row['mine'] or user.get('admin')) and row in pending and pending.index(row)<len(pending)-1)
        return {'user': user, 'tasks': rows, 'nodes': list(self.nodes), 'cards_per_node': self.capacity, 'server_time': now, 'unified': True, 'can_urgent':can_urgent, 'normal_max_seconds':self.normal_max_seconds, 'night_carryover_cutoff_hour':self.night_carryover_cutoff_hour, 'missed_claim_limit':self.missed_claim_limit,
                'alerts':alerts, 'daily_reset_enabled':self.daily_reset, 'last_daily_reset':reset,
                'reservation_buffer_seconds': self.buffer,
                'reservation_window': {'start': now, 'end': self.normal_night_end(now) if self.midnight_separation else self.cutoff(now), 'timezone': str(self.timezone),
                                       'start_dates': [(datetime.fromtimestamp(now, self.timezone).date() + timedelta(days=i)).isoformat() for i in (0, 1)],
                                       'night_windows': self.night_windows(now)}}

    def enqueue(self, user, data):
        if data.get('notifications_ready') is not True:
            raise ControlError(400, '请先在主页开启桌面通知并通过测试，再加入队列')
        if self.capacity != 8 and data.get('cards_per_node') != self.capacity:
            raise ControlError(409, '卡位配置已更新，请刷新页面后重新预约')
        cards, seconds = data.get('cards'), data.get('requested_seconds')
        dual, night, start = data.get('dual', False), data.get('night', False), data.get('not_before', 0)
        urgent=data.get('urgent',False)
        if type(urgent) is not bool:raise ControlError(400,'无效紧急标记')
        if urgent and night:raise ControlError(400,'只有普通任务可以设为紧急')
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
                if cards not in tuple(n for n in (2, 4, 8, 16, 32) if n <= 2*self.capacity):
                    raise ControlError(400, '双机合计卡数只能选择 '+ '、'.join(str(n) for n in (2,4,8,16,32) if n<=2*self.capacity)+'，每台平均分配')
                cards //= 2
            else:
                fixed_node = '' if machine == 'auto' else machine
                if not 1 <= cards <= self.capacity:
                    raise ControlError(400, f'单机卡数只能选择 1–{self.capacity}')
        elif not 1 <= cards <= self.capacity or (dual and cards != self.capacity):
            # Legacy form uses a per-node count and supports full-node dual requests.
            raise ControlError(400, f'旧版请求请使用单机 1–{self.capacity} 卡或双机各 {self.capacity} 卡')
        if type(seconds) is not int or seconds < 60 or seconds % 60 or (not night and seconds > self.normal_max_seconds) or (night and seconds > 13*3600):
            normal_limit=(str(self.normal_max_seconds//3600)+' 小时' if self.normal_max_seconds%3600==0 else str(self.normal_max_seconds//60)+' 分钟')
            raise ControlError(400, '请按整分钟填写时长，至少 1 分钟；普通任务最长 '+normal_limit+'，夜间任务最长 13 小时')
        backfill=self.validate_backfill(data.get('backfill_min_seconds',0),seconds)
        if backfill and (night or not self.strict_nights):raise ControlError(400,'只有普通任务支持短时插空')
        if type(start) not in (int, float) or not math.isfinite(start) or start < 0:
            raise ControlError(400, '预约开始时间无效')
        if night and start <= now:
            raise ControlError(400, '夜间预约需填写未来开始时间')
        if night and not any(window['start'] <= start < window['end'] for window in self.night_windows(now)):
            raise ControlError(400, '夜间任务开始时间须在 00:00 至 09:30 前' if self.midnight_separation else '夜间任务开始时间须在晚上 20:00 至次日 09:00 前')
        if not night and start:
            raise ControlError(400, '普通任务无需填写预约开始时间')
        avoid=self.validate_avoid_slots(data.get('avoid_slots',[24,25,26,27,35,36,37,38])) if self.strict_nights and not night else []
        # Exactly 09:00 belongs to the preceding night, not the following one.
        finish_before = self.night_cutoff(start if self.midnight_separation else start - 0.001) if night else (0 if self.midnight_separation else self.cutoff(now))
        if night and self.night_carryover_cutoff_hour is not None:
            deadline=self.night_hard_cutoff({'not_before':start,'finish_before':finish_before})
            seconds=math.floor(deadline-start) if self.midnight_separation else min(seconds,math.floor(deadline-start))
        if not night and not self.midnight_separation and max(now, start) + seconds > finish_before:
            raise ControlError(400, '预约开始和预计结束时间都必须在次日上午 9 点前（含 9 点）')
        with self.store.db() as db:
            if urgent and not db.execute('SELECT 1 FROM unified_urgent_users WHERE employee=?',(user.get('employee',''),)).fetchone():
                raise ControlError(403,'当前账号不在紧急用户名单中')
            self._reset_if_due(db, now)
            count = db.execute("SELECT COUNT(*) FROM unified_tasks WHERE account=? AND state IN ('queued','ready','active')", (user['id'],)).fetchone()[0]
            if count >= 5:
                raise ControlError(409, '每人最多保留 5 个未结束预约')
            job = secrets.token_hex(16)
            order = db.execute('SELECT COALESCE(MAX(queue_order),0)+1 FROM unified_tasks').fetchone()[0]
            allocation={};night_fixed=0
            if night and self.strict_nights:
                from queue_night_policy import reserve_night
                reservations=[]
                for other in db.execute("SELECT * FROM unified_tasks WHERE not_before>0 AND state IN ('queued','ready','active') ORDER BY queue_order,rowid"):
                    other=dict(other);begin=other['activated'] if other['state']=='active' and not other['night_fixed'] else other['not_before']
                    cards_in_use=json.loads(other['allocations'])
                    if not cards_in_use:
                        # Legacy unassigned reservations must not lose their
                        # place to a new fixed reservation of the same resource.
                        possible=[other['fixed_node']] if other['fixed_node'] else list(self.nodes)
                        cards_in_use={node:self.nodes[node] for node in possible}
                    reservations.append({'start':begin,'end':begin+other['requested_seconds'],'allocations':cards_in_use})
                allocation=reserve_night(self.nodes,reservations,start,seconds,cards,dual,fixed_node)
                if allocation is None:
                    raise ControlError(409,'所选夜间时间段没有足够的固定卡位，请调整机器、卡数或开始时间')
                night_fixed=1
            db.execute('''INSERT INTO unified_tasks(id,account,title,state,created,requested_seconds,cards,dual,not_before,finish_before,fixed_node,queue_order)
                VALUES (?,?,?,'queued',?,?,?,?,?,?,?,?)''', (job, user['id'], '', now, seconds, cards, int(dual), start, finish_before, fixed_node, order))
            if night_fixed:
                db.execute('UPDATE unified_tasks SET night_fixed=1,allocations=?,night_requested_start=? WHERE id=?',(json.dumps(allocation),start,job))
            if avoid:
                db.execute('UPDATE unified_tasks SET avoid_slots=? WHERE id=?',(json.dumps(avoid),job))
            if backfill:
                db.execute('UPDATE unified_tasks SET backfill_min_seconds=? WHERE id=?',(backfill,job))
            if urgent:
                db.execute('UPDATE unified_tasks SET urgent=1 WHERE id=?',(job,))
                self._order_urgent(db,job,True)
            self._schedule(db, now)
        return job

    @staticmethod
    def _order_urgent(db,job,enabled):
        rows=db.execute("SELECT id,queue_order,urgent FROM unified_tasks WHERE not_before=0 AND state IN ('queued','ready') ORDER BY queue_order,rowid").fetchall()
        current=next((row for row in rows if row['id']==job),None)
        if current is None:return
        others=[row for row in rows if row['id']!=job]
        urgent=[row for row in others if row['urgent']]
        normal=[row for row in others if not row['urgent']]
        ordered=urgent+([current] if enabled else [])+normal+([] if enabled else [current])
        for slot,row in zip(rows,ordered):db.execute('UPDATE unified_tasks SET queue_order=? WHERE id=?',(slot['queue_order'],row['id']))

    def urgent_users(self):
        with self.store.db() as db:
            return [dict(row) for row in db.execute('SELECT employee,name FROM unified_urgent_users ORDER BY employee')]

    def set_urgent_user(self,employee,name,enabled):
        with self.store.db() as db:
            if enabled:db.execute('INSERT OR REPLACE INTO unified_urgent_users(employee,name) VALUES(?,?)',(employee,name))
            else:db.execute('DELETE FROM unified_urgent_users WHERE employee=?',(employee,))

    def set_urgent(self,user,job,enabled):
        if not isinstance(job,str) or type(enabled) is not bool:raise ControlError(400,'无效紧急标记')
        with self.store.db() as db:
            row=db.execute('SELECT * FROM unified_tasks WHERE id=?',(job,)).fetchone()
            if not row:raise ControlError(404,'预约不存在')
            if row['account']!=user['id']:raise ControlError(403,'只能修改自己的预约')
            if row['not_before'] or row['state'] not in ('queued','ready'):raise ControlError(409,'只能修改尚未开始的普通任务')
            if enabled and not db.execute('SELECT 1 FROM unified_urgent_users WHERE employee=?',(user.get('employee',''),)).fetchone():
                raise ControlError(403,'当前账号不在紧急用户名单中')
            if bool(row['urgent'])==enabled:return
            db.execute('UPDATE unified_tasks SET urgent=? WHERE id=?',(int(enabled),job))
            self._order_urgent(db,job,enabled)
            self._schedule(db,time.time())

    @staticmethod
    def validate_backfill(value,requested):
        if type(value) is not int or value<0 or (value and (value<300 or value%300 or value>requested)):
            raise ControlError(400,'最短插空时长须为 5 分钟的整数倍，且不超过预约时长')
        return value

    def update_backfill(self,user,job,value):
        if not self.strict_nights or not isinstance(job,str):raise ControlError(400,'当前部署不支持短时插空')
        with self.store.db() as db:
            row=db.execute('SELECT * FROM unified_tasks WHERE id=?',(job,)).fetchone()
            if not row:raise ControlError(404,'预约不存在')
            if row['account']!=user['id'] and not user.get('admin'):raise ControlError(403,'只能修改自己的预约')
            if row['not_before'] or row['state'] not in ('queued','ready'):raise ControlError(409,'只能修改尚未开始的普通任务')
            value=self.validate_backfill(value,row['requested_seconds'])
            now=time.time()
            if row['state']=='ready' and row['backfill_end_before'] and (not value or row['backfill_end_before']-now<value):
                db.execute("UPDATE unified_tasks SET backfill_min_seconds=?,state='queued',allocations='{}',claim_before=0,ready_since=0,offer_id='',offer_end_before=0,backfill_end_before=0 WHERE id=?",(value,job))
            else:
                db.execute('UPDATE unified_tasks SET backfill_min_seconds=? WHERE id=?',(value,job))
            self._schedule(db,now)

    def update_avoid_slots(self,user,job,slots):
        if not self.strict_nights or not isinstance(job,str):raise ControlError(400,'当前部署不支持避开时段')
        slots=self.validate_avoid_slots(slots);now=time.time()
        with self.store.db() as db:
            row=db.execute('SELECT * FROM unified_tasks WHERE id=?',(job,)).fetchone()
            if not row:raise ControlError(404,'预约不存在')
            if row['account']!=user['id'] and not user.get('admin'):raise ControlError(403,'只能修改自己的预约')
            if row['not_before'] or row['state'] not in ('queued','ready'):raise ControlError(409,'只能修改尚未开始的普通任务')
            db.execute('UPDATE unified_tasks SET avoid_slots=? WHERE id=?',(json.dumps(slots),job))
            self._schedule(db,now)

    @staticmethod
    def allowed_employee(employee):
        if not isinstance(employee,str):
            raise ControlError(400, '请填写有效工号')
        employee = employee.strip().lower()
        if not re.fullmatch(r'[a-z][0-9]{8}', employee):
            raise ControlError(400, '请填写有效工号')
        return employee

    def check_owner(self, user, job):
        if not isinstance(job,str):
            raise ControlError(400, '无效的预约')
        with self.store.db() as db:
            row=db.execute('SELECT account,state,allowed_employees FROM unified_tasks WHERE id=?', (job,)).fetchone()
            if not row:raise ControlError(404,'预约不存在')
            if row['account']!=user['id']:raise ControlError(403,'只有预约申请人可以管理允许用户')
            if row['state'] not in ('queued','ready','active'):raise ControlError(409,'预约已结束')
            return json.loads(row['allowed_employees'])

    def allowed_user(self, user, job, employee, operation):
        if not isinstance(job, str) or operation not in ('add', 'remove'):
            raise ControlError(400, '无效的允许用户操作')
        employee = self.allowed_employee(employee)
        with self.store.db() as db:
            self._reset_if_due(db, time.time())
            row = db.execute('SELECT account,state,allowed_employees FROM unified_tasks WHERE id=?', (job,)).fetchone()
            if not row:
                raise ControlError(404, '预约不存在')
            if row['account'] != user['id']:
                raise ControlError(403, '只有预约申请人可以管理允许用户')
            if employee == user.get('employee', '').lower():
                raise ControlError(400, '申请人无需添加自己')
            if row['state'] not in ('queued', 'ready', 'active'):
                raise ControlError(409, '预约已结束')
            allowed = json.loads(row['allowed_employees'])
            if operation == 'add' and employee not in allowed:
                if len(allowed) >= 32:
                    raise ControlError(400, '每条预约最多添加 32 位允许用户')
                allowed.append(employee)
            elif operation == 'remove':
                allowed = [item for item in allowed if item != employee]
            db.execute('UPDATE unified_tasks SET allowed_employees=? WHERE id=?', (json.dumps(allowed), job))

    def action(self, user, job, action, offer_id=None, extension_minutes=None):
        if not isinstance(job, str) or action not in ('claim', 'finish', 'cancel', 'priority-up', 'priority-down', 'priority-top', 'extend'):
            raise ControlError(400, '无效任务操作')
        now = time.time()
        with self.store.db() as db:
            self._schedule(db, now)
            row = db.execute('SELECT * FROM unified_tasks WHERE id=?', (job,)).fetchone()
            if not row:
                raise ControlError(404, '任务不存在')
            if action in ('priority-up','priority-down','priority-top'):
                if action != 'priority-down' and not user.get('admin'):
                    raise ControlError(403,'仅管理员可调整排队优先级')
                if row['urgent']:
                    raise ControlError(409,'紧急任务按进入紧急状态的顺序排列')
                if action == 'priority-down' and row['account'] != user['id'] and not user.get('admin'):
                    raise ControlError(403,'只能调低自己的预约')
                if action == 'priority-down' and row['state'] == 'ready' and (not offer_id or offer_id != row['offer_id']):
                    raise ControlError(409,'这次资源分配已失效，请刷新后查看当前排队状态')
                if action == 'priority-down' and row['state'] == 'queued' and offer_id:
                    raise ControlError(409,'这次资源分配已失效，请刷新后查看当前排队状态')
                states = "('queued','ready')" if action == 'priority-down' else "('queued')"
                waiting=db.execute("SELECT id,queue_order FROM unified_tasks WHERE state IN "+states+" AND (not_before>0)=? AND urgent=? ORDER BY queue_order,rowid", (int(bool(row['not_before'])),row['urgent'])).fetchall()
                index=next((i for i,r in enumerate(waiting) if r['id']==job),None)
                if index is None:
                    raise ControlError(409,'仅等待中的预约可调整优先级')
                if action=='priority-top':
                    if index==0:
                        raise ControlError(409,'已在等待队列首位')
                    db.execute('UPDATE unified_tasks SET queue_order=? WHERE id=?',(waiting[0]['queue_order'],job))
                    for position in range(index):
                        db.execute('UPDATE unified_tasks SET queue_order=? WHERE id=?',(waiting[position+1]['queue_order'],waiting[position]['id']))
                    self._schedule(db,now)
                    return
                other=index+(-1 if action=='priority-up' else 1)
                if not 0<=other<len(waiting):
                    raise ControlError(409,'已到等待队列边界')
                if row['state'] == 'ready':
                    db.execute("UPDATE unified_tasks SET state='queued',allocations='{}',claim_before=0,ready_since=0,offer_id='' WHERE id=?", (job,))
                db.execute('UPDATE unified_tasks SET queue_order=? WHERE id=?',(waiting[other]['queue_order'],job))
                db.execute('UPDATE unified_tasks SET queue_order=? WHERE id=?',(waiting[index]['queue_order'],waiting[other]['id']))
                self._schedule(db,now)
                return
            collaborator=user.get('employee','') in json.loads(row['allowed_employees'])
            if row['account'] != user['id'] and not user.get('admin') and not (collaborator and action in ('claim','finish','extend')):
                raise ControlError(403, '只能操作自己的预约')
            if action=='extend':
                if not self.overtime_control or row['not_before'] or row['state']!='active' or type(extension_minutes) is not int or extension_minutes not in (10,20,30):
                    raise ControlError(400,'请选择可延长的普通任务和有效时长')
                end=row['activated']+row['requested_seconds']
                if now<end or now>=end+600:raise ControlError(409,'只能在超时后的 10 分钟内申请延长')
                limit=self.normal_night_end(row['activated'])
                allocations=json.loads(row['allocations'])
                for night in db.execute("SELECT * FROM unified_tasks WHERE night_fixed=1 AND not_before>? AND state IN ('queued','ready','active')",(now,)):
                    if self.normal_priority:continue
                    reserved=json.loads(night['allocations'])
                    if any(set(cards).intersection(reserved.get(node,[])) for node,cards in allocations.items()):
                        limit=min(limit,night['not_before']-(0 if self.midnight_separation else self.buffer))
                new_end=now+extension_minutes*60
                if row['backfill_end_before'] and new_end>row['backfill_end_before']:
                    raise ControlError(409,'插空任务不能延长到影响前序预约，请按时结束登记并自行停止任务')
                if new_end>limit:raise ControlError(409,'延长后会超过午夜 0 点截止时间，请选择更短时间或结束任务' if self.midnight_separation else '延长后会超过凌晨 '+('1' if self.normal_priority else '2')+' 点截止时间，请选择更短时间或结束任务')
                db.execute('UPDATE unified_tasks SET requested_seconds=?,extension_minutes=?,extension_total_minutes=extension_total_minutes+?,extension_at=?,extension_count=extension_count+1 WHERE id=?',
                           (math.ceil(new_end-row['activated']),extension_minutes,extension_minutes,now,job))
                if row['extension_count']+1>=2:
                    owner=db.execute('SELECT employee,name FROM accounts WHERE id=?',(row['account'],)).fetchone()
                    if owner:
                        payload=json.dumps(dict(count=row['extension_count']+1,minutes=extension_minutes,
                            total=row['extension_total_minutes']+extension_minutes,allocations=allocations),ensure_ascii=False)
                        db.execute('INSERT INTO unified_notice_events VALUES (?,?,?,?,?,?,?,?)',
                            ('extension:'+job+':'+str(row['extension_count']+1),'extension',job,
                             owner['employee'],owner['name'],row['allowed_employees'],now,payload))
                self._schedule(db,now)
                return 'extended'
            if action == 'claim' and row['state'] == 'ready':
                if offer_id is not None and offer_id != row['offer_id']:
                    raise ControlError(409, '这次资源分配已失效，请刷新后查看当前排队状态')
                if not self.midnight_separation and not row['not_before'] and row['finish_before'] and now + row['requested_seconds'] > row['finish_before']:
                    raise ControlError(409, '剩余预约时段不足，请取消后重新预约')
                seconds=row['requested_seconds']
                if self.strict_nights and not row['not_before']:
                    if self.midnight_separation and self.normal_allowed_from(now)>now:
                        raise ControlError(409,'00:00 至 09:30 不允许普通任务开始')
                    if not self.midnight_separation and now>=self.normal_one_am(row['created']):
                        raise ControlError(409,'普通任务最晚须在'+('午夜 0 点' if self.normal_priority else '凌晨 1 点')+'前开始')
                    seconds=min(seconds,math.floor(self.normal_night_end(now if self.midnight_separation else row['created'])-now))
                    if row['offer_end_before']:
                        seconds=min(seconds,math.floor(row['offer_end_before']-now))
                    if row['backfill_end_before'] and seconds<row['backfill_min_seconds']:
                        raise ControlError(409,'插空剩余时间不足最低接受时长，请继续等待')
                    if seconds<60:raise ControlError(409,'剩余使用时段不足 1 分钟，请重新排队')
                db.execute("UPDATE unified_tasks SET state='active',activated=?,requested_seconds=? WHERE id=?", (now,seconds,job))
            elif action == 'finish' and row['state'] == 'active':
                db.execute('DELETE FROM unified_tasks WHERE id=?', (job,))
                db.execute('DELETE FROM confirmations WHERE job=?', (job,))
                db.execute('DELETE FROM reservations WHERE id=?', (job,))
            elif action == 'cancel' and row['state'] in ('queued', 'ready'):
                if self.normal_priority and row['not_before'] and row['night_takeover']:
                    raise ControlError(409,'夜间资源已开始接管，不能取消；开始后可结束登记')
                db.execute('DELETE FROM unified_tasks WHERE id=?', (job,))
                db.execute('DELETE FROM confirmations WHERE job=?', (job,))
                db.execute('DELETE FROM reservations WHERE id=?', (job,))
            else:
                raise ControlError(409, '任务状态已变化，请刷新后重试')
            self._schedule(db, now)

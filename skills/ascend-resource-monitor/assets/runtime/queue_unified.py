"""Transactional, manual-only reservations across two eight-card nodes."""
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
    def __init__(self, store, nodes, buffer_seconds=600, timezone='UTC', daily_reset=False):
        self.store = store
        self.timezone = dt_timezone.utc if timezone == 'UTC' else ZoneInfo(timezone)
        self.daily_reset = daily_reset
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
                                     ('missed_claims', 'INTEGER NOT NULL DEFAULT 0'),
                                     ('allowed_employees', "TEXT NOT NULL DEFAULT '[]'"),
                                     ('night_seen_usage', 'INTEGER NOT NULL DEFAULT 0')]:
                if name not in columns:
                    db.execute('ALTER TABLE unified_tasks ADD COLUMN '+name+' '+definition)
            db.execute('CREATE TABLE IF NOT EXISTS unified_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS unified_continuing (node TEXT, employee TEXT, cutoff REAL, PRIMARY KEY(node,employee))')
            db.execute('CREATE TABLE IF NOT EXISTS unified_shared_continuing (node TEXT, employee TEXT, cutoff REAL, cards TEXT, PRIMARY KEY(node,employee))')
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

    def night_cutoff(self, start):
        local = datetime.fromtimestamp(start, self.timezone)
        day = local.date() if local.hour < 9 else local.date() + timedelta(days=1)
        return datetime.combine(day, daytime(9), self.timezone).timestamp()

    def normal_two_am(self, created):
        local = datetime.fromtimestamp(created, self.timezone)
        day = local.date() if local.hour < 2 else local.date() + timedelta(days=1)
        return datetime.combine(day, daytime(2), self.timezone).timestamp()

    def night_windows(self, now):
        day = datetime.fromtimestamp(now, self.timezone).date()
        result = []
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
        expired=[row['id'] for row in db.execute("SELECT id,not_before,finish_before FROM unified_tasks WHERE not_before>0 AND state IN ('queued','ready')")
                 if (row['finish_before'] or self.night_cutoff(row['not_before']-0.001))<=boundary]
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

    def alerts(self, rows, user, now, reset, blocked, dependent, joint):
        result = []
        if reset and (reset['count'] or reset.get('cancelled_nights')) and 0 <= now-reset['at'] < 600:
            result.append({'id':'daily-reset:'+str(reset['at']), 'kind':'reset',
                           'message':'上午 9 点已清空普通任务预约，取消未开始的夜间预约；已开始的夜间任务保留，实际任务未停止。'})
        if not user:
            return result
        for row in rows:
            if row['state']!='active' or not row['activated'] or now < row['activated']+row['requested_seconds']:
                continue
            label=row['name']+'（'+row['employee']+'）'
            bottleneck=bool(blocked.get(row['id']))
            jointly_blocking=bool(joint.get(row['id'])) and not bottleneck
            indirectly_blocking=bool(dependent.get(row['id'])) and not bottleneck and not jointly_blocking
            if row['account']==user['id']:
                interval=300 if bottleneck or jointly_blocking or indirectly_blocking else 900
                status='blocking' if bottleneck else 'joint' if jointly_blocking else 'dependent' if indirectly_blocking else 'nonblocking'
                message=('当前正阻挡后续预约，请尽快自行停止任务并结束登记。' if bottleneck else
                         '与其他超时任务共同影响后续预约，请尽快自行停止任务并结束登记。' if jointly_blocking else
                         '占用等待中预约所需资源；其他资源也可能尚未释放，请尽快自行停止任务并结束登记。' if indirectly_blocking else
                         '目前不是可立即开始预约的瓶颈；用完请自行停止任务并结束登记。')
                result.append({'id':'usage-overdue:'+row['id']+':owner:'+status,
                               'job':row['id'],'repeat_seconds':interval,
                               'kind':'overdue' if bottleneck else 'overdue-joint' if jointly_blocking else 'overdue-dependent' if indirectly_blocking else 'overdue-nonblocking',
                               'message':label+'的预约已使用超时，'+message})
            if now-row['activated']-row['requested_seconds'] > 300:
                for waiting in blocked.get(row['id'],[]):
                    if waiting['account']==user['id'] and user['id']!=row['account']:
                        result.append({'id':'usage-overdue:'+row['id']+':next:'+waiting['id'],'kind':'overdue-next',
                                       'message':label+'的预约已超时超过 5 分钟；预计其释放资源后，你的预约可以立即开始。'})
                if not bottleneck:
                    for waiting in dependent.get(row['id'],[]):
                        if waiting['account']==user['id'] and user['id']!=row['account']:
                            shared=waiting in joint.get(row['id'],[])
                            result.append({'id':'usage-overdue:'+row['id']+':dependent-next:'+waiting['id'],
                                           'kind':'overdue-joint-next' if shared else 'overdue-dependent-next',
                                           'message':label+'的预约已超时超过 5 分钟，'+('与其他超时任务共同影响你前方的资源安排' if shared else '可能影响你前方的资源安排，其他资源也可能尚未释放')+'；请按队列顺序等待，预计开始时间以页面为准。'})
        return result

    def _expire_offers(self, db, now):
        pending = [dict(row) for row in db.execute("SELECT * FROM unified_tasks WHERE state IN ('queued','ready') ORDER BY queue_order,rowid")]
        expired = [row for row in pending if row['state']=='ready' and
                   ((row['claim_before'] and row['claim_before']<=now) or
                    (not row['not_before'] and now >= self.normal_two_am(row['created'])))]
        missed = {row['id'] for row in expired if row['ready_since'] and now>=row['ready_since']+600}
        for row in expired:
            db.execute("UPDATE unified_tasks SET state='queued',allocations='{}',claim_before=0,ready_since=0,offer_id='',missed_claims=missed_claims+? WHERE id=?",
                       (int(row['id'] in missed), row['id']))
        if not missed:
            return
        # Move missed offers back within their own task type. Keep the slots
        # of other task types in place so expiry cannot reshuffle their priority.
        groups = {False: [], True: []}
        for row in pending:
            groups[bool(row['not_before'])].append(row)
        for night, group in groups.items():
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
            groups[night] = iter(order)
        for position, slot in enumerate(pending, 1):
            row = next(groups[bool(slot['not_before'])])
            db.execute('UPDATE unified_tasks SET queue_order=? WHERE id=?', (position, row['id']))

    def tick(self):
        with self.store.db() as db:
            self._schedule(db, time.time())

    def auto_finish_nights(self, monitor, now=None):
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
            if row['state'] in ('ready', 'active'):
                end = (row['activated'] + row['requested_seconds'] + self.buffer
                       if row['state'] == 'active' and row['activated'] and row['requested_seconds'] else
                       row['claim_before'] + row['requested_seconds'] + self.buffer
                       if row['state'] == 'ready' and row['claim_before'] else None)
                if row['state'] == 'active' and row['activated'] and row['requested_seconds'] and now >= row['activated'] + row['requested_seconds']:
                    end = None
                direct_end = end if end is None else end-self.buffer
                for node, cards in json.loads(row['allocations']).items():
                    busy.append({'node': node, 'cards': cards, 'start': row['activated'] or row['created'], 'end': end, 'direct_end': direct_end})
            else:
                item = {'id': row['id'], 'cards': row['cards'], 'dual': bool(row['dual']),
                        'seconds': row['requested_seconds'] or 3600, 'not_before': row['not_before'],
                        'night': bool(row['not_before']),
                        'finish_before': row['finish_before'] if not row['not_before'] else 0,
                        'start_before': self.normal_two_am(row['created']) if not row['not_before'] else 0}
                if row['fixed_node']:
                    item['allowed_nodes'] = [row['fixed_node']]
                waiting.append(item)
        return busy, waiting

    def _schedule(self, db, now):
        self._reset_if_due(db, now)
        # Upgrade previously offered night reservations without dropping their allocations.
        db.execute("UPDATE unified_tasks SET state='active',activated=CASE WHEN ready_since>0 AND ready_since<=? THEN ready_since ELSE ? END,claim_before=0,ready_since=0,offer_id='' WHERE state='ready' AND not_before>0", (now,now))
        self._expire_offers(db, now)
        rows = [dict(row) for row in db.execute("SELECT * FROM unified_tasks WHERE state IN ('ready','active','queued') ORDER BY queue_order,rowid")]
        # A due night reservation can take over expired night allocations without
        # consulting process inventory. Morning carryovers still require manual release.
        expired={row['id']:row for row in rows if row['state']=='active' and row['not_before']
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
        previous=[]
        for row in rows:
            prediction = predictions.get(row['id'])
            if prediction and prediction['start'] is not None and prediction['start'] <= now:
                if row['not_before']:
                    db.execute("UPDATE unified_tasks SET state='active',activated=?,allocations=?,claim_before=0,ready_since=0,offer_id='' WHERE id=? AND state='queued'",
                               (now, json.dumps(prediction['allocations']), row['id']))
                    previous.append(prediction)
                    continue
                deadlines=[now+600]
                if not row['not_before']:
                    deadlines.append(self.normal_two_am(row['created']))
                if row['finish_before'] and not row['not_before']:
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
            enabled = [queued[p['id']] for p in released
                       if p['id'] not in immediate and p['start'] is not None and p['start'] <= now]
            # When multiple tasks occupy a waiter's cards, the last estimated
            # release is a bottleneck even though no single release helps now.
            finish = row['activated'] + row['requested_seconds'] if row['requested_seconds'] else 0
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
        """Estimate remaining overtime against related active resource blockers.

        Compare the queue with this allocation released versus held indefinitely.
        Only use a competing active allocation whose estimated end actually
        determines the affected waiter's planned start, excluding its buffer.
        """
        queued={row['id']:row for row in rows if row['state']=='queued'}
        if not queued:return {}
        active=[row for row in rows if row['state']=='active' and row['activated'] and row['requested_seconds']]
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
            if references:
                result[row['id']]=max(0,min(references)-max(now,row['activated']+row['requested_seconds']))
        return result

    def view(self, user):
        now = time.time()
        account = user['id'] if user else ''
        with self.store.db() as db:
            predictions = self._schedule(db, now)
            rows = [dict(row) for row in db.execute('''SELECT r.*,a.employee,a.name FROM unified_tasks r
                JOIN accounts a ON a.id=r.account WHERE r.state IN ('queued','ready','active')
                ORDER BY r.queue_order,r.rowid''')]
            reset_row=db.execute("SELECT value FROM unified_meta WHERE key='last_daily_reset'").fetchone()
            reset=json.loads(reset_row[0]) if reset_row else None
        blocked=self.bottlenecks(rows,now)
        dependent,joint=self.overdue_resource_dependencies(rows,now)
        allowances=self.overtime_allowances(rows,now,{**blocked,**dependent})
        alerts=self.alerts(rows,user,now,reset,blocked,dependent,joint)
        position = 0
        for row in rows:
            row['mine'] = row.pop('account') == account
            row.pop('title', None)
            row['allocations'] = json.loads(row['allocations'])
            row['allowed_employees'] = json.loads(row['allowed_employees'])
            if row['state'] in ('queued', 'ready'):
                position += 1
                row['position'] = position
            else:
                row['position'] = None
            row['can_claim'] = row['mine'] and row['state'] == 'ready'
            row['overdue_seconds'] = max(0,now-row['activated']-row['requested_seconds']) if row['state']=='active' and row['activated'] else 0
            row['overdue'] = bool(row['state']=='active' and row['activated'] and now>=row['activated']+row['requested_seconds'])
            row['bottleneck'] = bool(row['state']=='active' and row['id'] in blocked)
            row['overdue_bottleneck'] = bool(row['overdue'] and row['bottleneck'])
            row['overdue_joint_blocker'] = bool(row['overdue'] and row['id'] in joint and not row['bottleneck'])
            row['overdue_pending_blocker'] = bool(row['overdue'] and row['id'] in dependent and not row['bottleneck'] and not row['overdue_joint_blocker'])
            row['overtime_remaining_seconds'] = allowances.get(row['id'])
            row['claim_remaining_seconds'] = max(0, row['claim_before']-now) if row['state']=='ready' else 0
            prediction = predictions.get(row['id'], {})
            row['estimated_start'] = prediction.get('start') if row['state'] == 'queued' else None
            row['window_expired'] = bool(not row['not_before'] and row['finish_before'] and now + row['requested_seconds'] > row['finish_before'] and row['state'] != 'active')
            estimated = row['activated'] if row['state']=='active' else now if row['state']=='ready' else prediction.get('start')
            two_am = self.normal_two_am(row['created']) if not row['not_before'] else 0
            row['normal_start_after_two'] = bool(row['mine'] and two_am and row['state'] == 'queued' and estimated is None)
            row['normal_start_unknown'] = False
            row['night_end_overdue'] = bool(row['not_before'] and row['finish_before'] and (now > row['finish_before'] or (estimated is not None and estimated + row['requested_seconds'] > row['finish_before'])))
            row['morning_carryover'] = bool(row['state']=='active' and row['not_before'] and now >= (row['finish_before'] or self.night_cutoff(row['not_before']-0.001)))
            row['planned_allocations'] = prediction.get('allocations', {}) if row['state'] == 'queued' else {}
            row['node'] = ', '.join(row['allocations']) or row['fixed_node'] or '待分配'
            row['machine'] = 'dual' if row['dual'] else row['fixed_node'] or 'auto'
            row['total_cards'] = row['cards'] * (2 if row['dual'] else 1)
        for row in rows:
            waiting=[r for r in rows if r['state']=='queued' and bool(r['not_before'])==bool(row['not_before'])]
            pending=[r for r in rows if r['state'] in ('queued','ready') and bool(r['not_before'])==bool(row['not_before'])]
            row['can_raise_priority']=bool(user and user.get('admin') and row in waiting and waiting.index(row)>0)
            row['can_lower_priority']=bool(user and (row['mine'] or user.get('admin')) and row in pending and pending.index(row)<len(pending)-1)
        return {'user': user, 'tasks': rows, 'nodes': list(self.nodes), 'server_time': now, 'unified': True,
                'alerts':alerts, 'daily_reset_enabled':self.daily_reset, 'last_daily_reset':reset,
                'reservation_buffer_seconds': self.buffer,
                'reservation_window': {'start': now, 'end': self.cutoff(now), 'timezone': str(self.timezone),
                                       'start_dates': [(datetime.fromtimestamp(now, self.timezone).date() + timedelta(days=i)).isoformat() for i in (0, 1)],
                                       'night_windows': self.night_windows(now)}}

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
        if type(seconds) is not int or seconds < 60 or seconds % 60 or (not night and seconds > 7200) or (night and seconds > 13*3600):
            raise ControlError(400, '请按整分钟填写时长，至少 1 分钟；普通任务最长 2 小时，夜间任务最长 13 小时')
        if type(start) not in (int, float) or not math.isfinite(start) or start < 0:
            raise ControlError(400, '预约开始时间无效')
        if night and start <= now:
            raise ControlError(400, '夜间预约需填写未来开始时间')
        if night and not any(window['start'] <= start < window['end'] for window in self.night_windows(now)):
            raise ControlError(400, '夜间任务开始时间须在晚上 20:00 至次日 09:00 前')
        if not night and start:
            raise ControlError(400, '普通任务无需填写预约开始时间')
        # Exactly 09:00 belongs to the preceding night, not the following one.
        finish_before = self.night_cutoff(start - 0.001) if night else self.cutoff(now)
        if not night and max(now, start) + seconds > finish_before:
            raise ControlError(400, '预约开始和预计结束时间都必须在次日上午 9 点前（含 9 点）')
        with self.store.db() as db:
            self._reset_if_due(db, now)
            count = db.execute("SELECT COUNT(*) FROM unified_tasks WHERE account=? AND state IN ('queued','ready','active')", (user['id'],)).fetchone()[0]
            if count >= 5:
                raise ControlError(409, '每人最多保留 5 个未结束预约')
            job = secrets.token_hex(16)
            order = db.execute('SELECT COALESCE(MAX(queue_order),0)+1 FROM unified_tasks').fetchone()[0]
            db.execute('''INSERT INTO unified_tasks(id,account,title,state,created,requested_seconds,cards,dual,not_before,finish_before,fixed_node,queue_order)
                VALUES (?,?,?,'queued',?,?,?,?,?,?,?,?)''', (job, user['id'], '', now, seconds, cards, int(dual), start, finish_before, fixed_node, order))
            self._schedule(db, now)
        return job

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
            row=db.execute('SELECT account,state FROM unified_tasks WHERE id=?', (job,)).fetchone()
            if not row:raise ControlError(404,'预约不存在')
            if row['account']!=user['id']:raise ControlError(403,'只有预约申请人可以管理允许用户')
            if row['state'] not in ('queued','ready','active'):raise ControlError(409,'预约已结束')

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

    def action(self, user, job, action, offer_id=None):
        if not isinstance(job, str) or action not in ('claim', 'finish', 'cancel', 'priority-up', 'priority-down', 'priority-top'):
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
                if action == 'priority-down' and row['account'] != user['id'] and not user.get('admin'):
                    raise ControlError(403,'只能调低自己的预约')
                if action == 'priority-down' and row['state'] == 'ready' and (not offer_id or offer_id != row['offer_id']):
                    raise ControlError(409,'这次资源分配已失效，请刷新后查看当前排队状态')
                if action == 'priority-down' and row['state'] == 'queued' and offer_id:
                    raise ControlError(409,'这次资源分配已失效，请刷新后查看当前排队状态')
                states = "('queued','ready')" if action == 'priority-down' else "('queued')"
                waiting=db.execute("SELECT id,queue_order FROM unified_tasks WHERE state IN "+states+" AND (not_before>0)=? ORDER BY queue_order,rowid", (int(bool(row['not_before'])),)).fetchall()
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
            if row['account'] != user['id'] and not user.get('admin'):
                raise ControlError(403, '只能操作自己的预约')
            if action == 'claim' and row['state'] == 'ready':
                if offer_id is not None and offer_id != row['offer_id']:
                    raise ControlError(409, '这次资源分配已失效，请刷新后查看当前排队状态')
                if not row['not_before'] and row['finish_before'] and now + row['requested_seconds'] > row['finish_before']:
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

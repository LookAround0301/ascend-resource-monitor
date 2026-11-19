"""Administrator reservation preemption. Never controls processes or containers."""
from datetime import datetime
from itertools import combinations
import hashlib
import json
import math
import re
import secrets
import time

from control_common import ControlError


def allocation_for(nodes, target, rows):
    """Prefer unused cards and reuse an already affected booking where possible."""
    allowed = json.loads(target['selected_nodes']) or ([target['fixed_node']] if target['fixed_node'] else list(nodes))
    held = {r['id']: (r, json.loads(r['allocations'])) for r in rows
            if r['id'] != target['id'] and r['state'] in ('active', 'ready')}
    allocation, affected = {}, set()
    count = target['cards']
    for _ in range(target['node_count']):
        candidates = []
        for node in allowed:
            if node in allocation:
                continue
            ordered = nodes[node]
            half = len(ordered)//2
            groups = [ordered[:half], ordered[half:]] if count <= half else [ordered]
            for group in groups:
                for cards in combinations(group, count):
                    victims = {job for job, (row, occupied) in held.items()
                               if set(cards).intersection(occupied.get(node, []))}
                    # Fixed nights are never displaced by this daytime feature.
                    if any(held[job][0]['not_before'] for job in victims):
                        continue
                    combined = affected | victims
                    score = (len(combined), sum(held[job][0]['state']=='active' for job in combined),
                             sum(sum(map(len, held[job][1].values())) for job in combined), node, cards)
                    candidates.append((score, node, cards, victims))
        if not candidates:
            raise ControlError(409, '所需资源与夜间预约冲突，不能抢占夜间预约')
        _, node, cards, victims = min(candidates)
        allocation[node] = list(cards)
        affected.update(victims)
    return allocation, [r for r in rows if r['id'] in affected]


def notice(record):
    payload = json.loads(record['payload'])
    return dict(id=record['id'], kind=record['kind'], job=record['job'],
                message=payload['message'], occurred_at=record['at'])


def emergency(queue, user, job, operation=None, revision=None):
    if not queue.admin_preemption_enabled:
        raise ControlError(404, '管理员紧急抢占未启用')
    if not user or user.get('admin') is not True:
        raise ControlError(403, '仅管理员可设置紧急抢占')
    if not isinstance(job, str) or not re.fullmatch(r'[0-9a-f]{32}', job):
        raise ControlError(400, '无效预约编号')
    if operation is not None and (not isinstance(operation, str) or not re.fullmatch(r'[0-9a-f]{32}', operation)
                                  or not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{64}', revision)):
        raise ControlError(400, '无效抢占确认，请重新预览')
    now = time.time()
    with queue.store.db() as db:
        if operation is not None:
            old = db.execute('SELECT * FROM unified_emergency_operations WHERE id=?', (operation,)).fetchone()
            if old:
                if (old['actor'], old['job'], old['revision']) != (user['id'], job, revision):
                    raise ControlError(409, '操作编号已用于其他抢占')
                return json.loads(old['result'])
        if not queue.midnight_separation or not 9 <= datetime.fromtimestamp(now, queue.timezone).hour < 24:
            raise ControlError(409, '普通任务紧急抢占仅限北京时间 09:00–24:00，保留夜间分隔')
        queue._schedule(db, now)
        rows = [dict(r) for r in db.execute('''SELECT r.*,a.employee,a.name FROM unified_tasks r
            JOIN accounts a ON a.id=r.account ORDER BY r.queue_order,r.rowid''')]
        target = next((r for r in rows if r['id']==job), None)
        if not target:
            raise ControlError(404, '预约不存在')
        if target['not_before'] or target['state'] not in ('queued', 'ready'):
            raise ControlError(409, '请选择尚未开始的普通预约')
        if target['state']=='ready' and target['emergency_at']:
            raise ControlError(409, '该紧急预约已分配，请刷新查看当前状态')
        deadline = queue.normal_night_end(now)
        seconds = min(target['requested_seconds'], math.floor(deadline-now))
        if seconds < 60:
            raise ControlError(409, '距午夜不足 1 分钟，不能分配普通任务')
        allocations, victims = allocation_for(queue.nodes, target, rows)
        # Stable across clock ticks; any booking/offer/owner change requires a
        # new preview. The immediate transaction also serializes concurrent admins.
        snapshot = json.dumps([rows, deadline], sort_keys=True, separators=(',', ':'))
        current_revision = hashlib.sha256(snapshot.encode()).hexdigest()
        preview = dict(job=job, revision=current_revision, allocations=allocations,
            effective_requested_seconds=seconds, owner=target['employee'], name=target['name'],
            affected=[dict(job=r['id'], employee=r['employee'], name=r['name'], state=r['state'],
                           allocations=json.loads(r['allocations'])) for r in victims])
        if operation is None:
            return preview
        if revision != current_revision:
            raise ControlError(409, '预约状态已变化，请重新预览受影响用户后确认')
        first = min([r['queue_order'] for r in rows] or [1])-len(victims)-2
        for index, row in enumerate(victims):
            db.execute('''UPDATE unified_tasks SET state='queued',activated=NULL,finished=NULL,
                allocations='{}',claim_before=0,ready_since=0,offer_id='',offer_end_before=0,
                backfill_end_before=0,backfill_no_buffer=0,extension_at=0,extension_minutes=0,
                emergency_at=0,preempted_at=?,queue_order=? WHERE id=?''', (now, first+index+1, row['id']))
            db.execute('DELETE FROM confirmations WHERE job=?', (row['id'],))
            db.execute('DELETE FROM reservations WHERE id=?', (row['id'],))
            resources = '；'.join(node+'：'+','.join(map(str,cards)) for node,cards in json.loads(row['allocations']).items())
            message = ('你的预约 '+row['id'][:8]+' 已被管理员紧急任务抢占，原预约资源（'+resources+
                       '）已释放登记。请求已放回等待队首，保留原需求和申请时长。请立即自行停止原任务并释放资源；'
                       '系统不会停止进程或容器。再次分配后请重新登记开始。')
            payload = dict(message=message, emergency_job=job, actor=user['id'],
                           previous_state=row['state'], allocations=json.loads(row['allocations']))
            db.execute('INSERT INTO unified_notice_events VALUES (?,?,?,?,?,?,?,?)',
                ('preempted:'+operation+':'+row['id'], 'preempted', row['id'], row['employee'],
                 row['name'], row['allowed_employees'], now, json.dumps(payload, ensure_ascii=False)))
        offer = secrets.token_hex(16)
        db.execute('''UPDATE unified_tasks SET state='ready',urgent=1,emergency_at=?,preempted_at=0,
            allocations=?,activated=NULL,claim_before=?,ready_since=?,offer_id=?,offer_end_before=?,
            backfill_end_before=0,backfill_no_buffer=0,queue_order=? WHERE id=?''',
            (now, json.dumps(allocations), min(now+600, deadline-60+0.001), now, offer, deadline, first, job))
        message = '管理员已将你的预约 '+job[:8]+' 设为紧急并立即分配资源。请先确认原用户已释放卡位，再在领取时限内登记开始；系统不会停止原进程或容器。'
        db.execute('INSERT INTO unified_notice_events VALUES (?,?,?,?,?,?,?,?)',
            ('emergency-allocated:'+operation, 'emergency-allocated', job, target['employee'],
             target['name'], target['allowed_employees'], now, json.dumps(dict(message=message, actor=user['id']), ensure_ascii=False)))
        result = dict(job=job, operation=operation, allocations=allocations,
            preempted=[r['id'] for r in victims],
            message='已设为紧急并立即分配资源；'+str(len(victims))+' 个预约回到队首，已生成用户通知。请核实原进程释放后再开始。')
        db.execute('INSERT INTO unified_emergency_operations VALUES (?,?,?,?,?,?)',
                   (operation, user['id'], job, revision, now, json.dumps(result, ensure_ascii=False)))
        queue._schedule(db, now)
        return result

"""Privacy, freshness, formatting, and persistent delivery/rate-limit helpers."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import math
import re
import sqlite3
import time

EMPLOYEE = re.compile(r'[a-z][0-9]{8}')
TZ = timezone(timedelta(hours=8))


def fresh(snapshot, cluster, now=None):
    now = time.time() if now is None else now
    try:
        age = now - float(snapshot['exported_at'])
        return (snapshot['version'] == 1 and snapshot['cluster'] == cluster
                and -30 <= age <= 90 and isinstance(snapshot['accounts'], list)
                and isinstance(snapshot['tasks'], list))
    except (KeyError, ValueError, TypeError):
        return False


def relevant(row, employee):
    return row.get('employee') == employee or any(
        p.get('employee') == employee for p in row.get('allowed_people', [])
        if isinstance(p, dict))


def stamp(value):
    return datetime.fromtimestamp(value, TZ).strftime('%m-%d %H:%M') if value else '待确认'


def resources(row):
    allocation = row.get('allocations') or row.get('planned_allocations') or {}
    if allocation:
        return '；'.join('%s: %s 号卡' % (node, ','.join(map(str, cards)))
                         for node, cards in allocation.items())
    return (row.get('fixed_node') or ('双机' if row.get('dual') else '单机')) + ' · %s 卡' % row.get('total_cards', row.get('cards', '?'))


def report(snapshot, employee, actions=False):
    lines = ['【%s 预约查询】（北京时间）' % snapshot['label']]
    rows = [row for row in snapshot['tasks'] if relevant(row, employee)]
    if not rows:
        return lines[0] + '\n你目前没有进行中或等待中的预约。'
    for index, row in enumerate(rows, 1):
        status = {'queued': '等待中', 'ready': '已分配，待开始', 'active': '使用中'}[row['state']]
        night = bool(row.get('not_before'))
        status += ' · 夜间任务' if night else ' · 普通任务'
        start = row.get('activated') if row['state'] == 'active' else row.get('estimated_start')
        text = '可登记开始' if row['state'] == 'ready' else stamp(start)
        if row['state'] == 'queued' and start:
            text = '预计 ' + text
        reference=(' [任务 '+str(row['id'])[:8]+']') if actions and row.get('id') else ''
        lines.append('%d.%s %s；%s；开始：%s' % (index,reference,status,resources(row),text))
    lines.append('手机可发送 /A5预约 释放 序号 或 /A5预约 延后 序号，按私聊确认指令执行。' if actions else '开始、结束和预约修改请到对应集群网页操作。')
    return '\n'.join(lines)


def parse_sender(body):
    if not isinstance(body, dict) or body.get('type') != 'Text':
        return None
    employee = body.get('sender')
    if (not isinstance(employee, str) or not EMPLOYEE.fullmatch(employee)
            or body.get('receiver') != employee or not isinstance(body.get('content'), str)):
        return None
    return employee


def init_db(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS deliveries (event TEXT PRIMARY KEY, created REAL NOT NULL, status TEXT NOT NULL)')


def allow_query(path, employee, now=None):
    now = time.time() if now is None else now
    with closing(sqlite3.connect(path, timeout=5)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("DELETE FROM deliveries WHERE status='query' AND created<?", (now - 120,))
        if db.execute("SELECT COUNT(*) FROM deliveries WHERE status='query' AND created>?", (now - 60,)).fetchone()[0] >= 20:
            return False
        key = 'query:%s:%s' % (employee, math.floor(now / 30))
        return bool(db.execute('INSERT OR IGNORE INTO deliveries VALUES (?,?,?)', (key, now, 'query')).rowcount)


def reserve_delivery(path, cluster, row, employee, legacy_employee=None):
    # Compatible with imported local A5 pilot records. No replay at cutover.
    old = 'ready:%s:%s' % (row['id'], row['offer_id'])
    key = 'ready:%s:%s:%s:%s' % (cluster, row['id'], row['offer_id'], employee)
    with closing(sqlite3.connect(path, timeout=5)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        if cluster == 'a5' and legacy_employee and employee == legacy_employee and db.execute(
                'SELECT 1 FROM deliveries WHERE event=?', (old,)).fetchone():
            return None
        inserted = db.execute('INSERT OR IGNORE INTO deliveries VALUES (?,?,?)',
                              (key, time.time(), 'attempted')).rowcount
        return key if inserted else None

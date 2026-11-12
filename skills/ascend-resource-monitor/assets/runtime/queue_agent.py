"""Idempotent agent operations and durable reservation events."""
import copy
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import time

from control_common import ControlError
from queue_mobile import EMPLOYEE, HEX32, TransactionStore

FIELDS = {'machine','cards','night','not_before','requested_seconds','avoid_slots',
          'backfill_min_seconds'}


def command(unified, store, payload):
    if set(payload) - {'mode','employee','request','operation','expires'}:
        raise ControlError(400, '无效预约请求')
    mode, employee, data = payload.get('mode'), payload.get('employee'), payload.get('request')
    if (mode not in ('agent_preview','agent_execute') or not isinstance(employee,str)
            or not EMPLOYEE.fullmatch(employee) or not isinstance(data,dict)
            or set(data)-FIELDS):
        raise ControlError(400, '无效预约请求')
    fingerprint=hashlib.sha256(json.dumps(data,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    now=time.time()
    with store.db() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS agent_reservation_receipts (
            operation TEXT PRIMARY KEY,employee TEXT NOT NULL,fingerprint TEXT NOT NULL,
            result TEXT NOT NULL,created REAL NOT NULL)''')
        account=db.execute('SELECT id,employee,name FROM accounts WHERE employee=?',(employee,)).fetchone()
        if account is None:raise ControlError(403,'请先在 A5 网页登录并建立个人账号')
        user=dict(account,admin=False,registered=True)
        if mode=='agent_execute':
            operation,expires=payload.get('operation'),payload.get('expires')
            if (not isinstance(operation,str) or not HEX32.fullmatch(operation)
                    or type(expires) not in (int,float) or not math.isfinite(expires)):
                raise ControlError(400,'无效确认操作')
            old=db.execute('SELECT * FROM agent_reservation_receipts WHERE operation=?',(operation,)).fetchone()
            if old:
                if (old['employee'],old['fingerprint'])!=(employee,fingerprint):
                    raise ControlError(409,'预约请求编号已被其他请求使用')
                return json.loads(old['result'])
            if not now<expires<=now+301:raise ControlError(409,'确认已过期，请重新申请')
        queue=copy.copy(unified);queue.store=TransactionStore(db)
        if mode=='agent_preview':db.execute('SAVEPOINT agent_preview')
        try:
            # The operator enables this endpoint only when server-side reminders
            # are configured. This is not a claim of browser notification setup.
            job=queue.enqueue(user,{**data,'notifications_ready':True,'cards_per_node':queue.capacity})
            row=db.execute('SELECT requested_seconds FROM unified_tasks WHERE id=?',(job,)).fetchone()
            effective_seconds=row['requested_seconds']
        finally:
            if mode=='agent_preview':
                db.execute('ROLLBACK TO agent_preview');db.execute('RELEASE agent_preview')
        if mode=='agent_preview':
            return dict(employee=employee,name=user['name'],request=data,effective_seconds=effective_seconds)
        result=dict(job=job,action='create',message='预约已创建，任务编号 '+job+'。请查询网页或小鲁班获取当前排队状态。')
        db.execute('INSERT INTO agent_reservation_receipts VALUES (?,?,?,?,?)',
                   (operation,employee,fingerprint,json.dumps(result,ensure_ascii=False),now))
        return result


def receipt(store, employee, operation):
    if not isinstance(operation,str) or not HEX32.fullmatch(operation):
        raise ControlError(400,'无效预约请求编号')
    with store.db() as db:
        table=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_reservation_receipts'").fetchone()
        row=db.execute('SELECT result FROM agent_reservation_receipts WHERE operation=? AND employee=?',(operation,employee)).fetchone() if table else None
        if row is None:raise ControlError(404,'尚未找到预约结果，请使用原请求编号核对')
        return json.loads(row['result'])


def action(unified, store, employee, operation, job, kind, offer_id='', minutes=None):
    """Apply one queue action and its idempotency receipt atomically."""
    if (not isinstance(employee,str) or not EMPLOYEE.fullmatch(employee)
            or not isinstance(operation,str) or not HEX32.fullmatch(operation)
            or not isinstance(job,str) or not HEX32.fullmatch(job)
            or kind not in ('claim','extend','finish')):
        raise ControlError(400,'无效 Agent 操作')
    if kind=='claim':
        if not isinstance(offer_id,str) or not offer_id or minutes is not None:raise ControlError(400,'开始操作需要当前分配编号')
    elif kind=='extend':
        if offer_id or type(minutes) is not int or minutes not in (10,20,30):raise ControlError(400,'无效延长时间')
    elif offer_id or minutes is not None:raise ControlError(400,'无效结束操作')
    fingerprint=hashlib.sha256(json.dumps(dict(job=job,kind=kind,offer_id=offer_id,minutes=minutes),sort_keys=True).encode()).hexdigest()
    with store.db() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS agent_action_receipts (
            operation TEXT PRIMARY KEY,employee TEXT NOT NULL,fingerprint TEXT NOT NULL,
            result TEXT NOT NULL,created REAL NOT NULL)''')
        old=db.execute('SELECT * FROM agent_action_receipts WHERE operation=?',(operation,)).fetchone()
        if old:
            if (old['employee'],old['fingerprint'])!=(employee,fingerprint):raise ControlError(409,'操作编号已用于其他请求')
            return json.loads(old['result'])
        account=db.execute('SELECT id,employee,name FROM accounts WHERE employee=?',(employee,)).fetchone()
        if not account:raise ControlError(403,'预约账号不存在')
        user=dict(account,admin=False,registered=True)
        queue=copy.copy(unified);queue.store=TransactionStore(db)
        outcome=queue.action(user,job,kind,offer_id or None,minutes)
        row=db.execute('SELECT id,state,activated,requested_seconds,allocations,not_before FROM unified_tasks WHERE id=?',(job,)).fetchone()
        result=dict(operation=operation,job=job,action=kind,outcome=outcome or 'ok',
                    reservation=(dict(row) if row else None))
        if result['reservation']:result['reservation']['allocations']=json.loads(result['reservation']['allocations'])
        db.execute('INSERT INTO agent_action_receipts VALUES (?,?,?,?,?)',
                   (operation,employee,fingerprint,json.dumps(result,ensure_ascii=False),time.time()))
        return result


def initialize_events(store):
    """Persist allocation transitions in the same transaction as the queue."""
    with store.db() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS agent_queue_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL, account TEXT NOT NULL,
            allowed_employees TEXT NOT NULL, state TEXT NOT NULL, offer_id TEXT NOT NULL,
            claim_before REAL NOT NULL, activated REAL NOT NULL, not_before REAL NOT NULL,
            requested_seconds INTEGER NOT NULL, allocations TEXT NOT NULL, occurred REAL NOT NULL)''')
        db.execute('CREATE INDEX IF NOT EXISTS agent_queue_events_account_seq ON agent_queue_events(account,seq)')
        db.execute('''CREATE TABLE IF NOT EXISTS agent_due_receipts (
            job TEXT NOT NULL, activated REAL NOT NULL, requested_seconds INTEGER NOT NULL,
            PRIMARY KEY(job,activated,requested_seconds))''')
        db.execute('''CREATE TRIGGER IF NOT EXISTS agent_queue_transition AFTER UPDATE OF state ON unified_tasks
            WHEN NEW.state IN ('ready','active') AND OLD.state<>NEW.state
            BEGIN
                INSERT INTO agent_queue_events(job,account,allowed_employees,state,offer_id,claim_before,
                     activated,not_before,requested_seconds,allocations,occurred)
                VALUES(NEW.id,NEW.account,NEW.allowed_employees,NEW.state,NEW.offer_id,NEW.claim_before,
                     COALESCE(NEW.activated,0),NEW.not_before,NEW.requested_seconds,NEW.allocations,
                     CAST(strftime('%s','now') AS REAL));
            END''')
        db.execute('''CREATE TRIGGER IF NOT EXISTS agent_due_before_delete BEFORE DELETE ON unified_tasks
            WHEN OLD.state='active' AND OLD.activated>0 AND OLD.requested_seconds>0
                 AND OLD.activated+OLD.requested_seconds<=(julianday('now')-2440587.5)*86400.0
            BEGIN
                INSERT OR IGNORE INTO agent_due_receipts VALUES(OLD.id,OLD.activated,OLD.requested_seconds);
                INSERT INTO agent_queue_events(job,account,allowed_employees,state,offer_id,claim_before,
                     activated,not_before,requested_seconds,allocations,occurred)
                SELECT OLD.id,OLD.account,OLD.allowed_employees,'due',OLD.offer_id,OLD.claim_before,
                     OLD.activated,OLD.not_before,OLD.requested_seconds,OLD.allocations,
                     (julianday('now')-2440587.5)*86400.0 WHERE changes()=1;
            END''')


def record_due_events(store, now=None):
    """Record each active reservation deadline once, including after a restart."""
    now=time.time() if now is None else now
    import sqlite3
    uri=Path(store.path).resolve().as_uri()+'?mode=ro'
    with closing(sqlite3.connect(uri,uri=True,timeout=5)) as check:
        due=check.execute('''SELECT 1 FROM unified_tasks t WHERE t.state='active' AND t.activated>0
             AND t.requested_seconds>0 AND t.activated+t.requested_seconds<=?
             AND NOT EXISTS(SELECT 1 FROM agent_due_receipts d WHERE d.job=t.id
                 AND d.activated=t.activated AND d.requested_seconds=t.requested_seconds) LIMIT 1''',(now,)).fetchone()
    if not due:return
    with store.db() as db:
        rows=db.execute('''SELECT * FROM unified_tasks WHERE state='active' AND activated>0
                           AND requested_seconds>0 AND activated+requested_seconds<=?''',(now,)).fetchall()
        for row in rows:
            inserted=db.execute('INSERT OR IGNORE INTO agent_due_receipts VALUES (?,?,?)',
                                (row['id'],row['activated'],row['requested_seconds'])).rowcount
            if inserted:
                db.execute('''INSERT INTO agent_queue_events(job,account,allowed_employees,state,offer_id,
                           claim_before,activated,not_before,requested_seconds,allocations,occurred)
                           VALUES (?,?,?,'due',?,?,?,?,?,?,?)''',
                           (row['id'],row['account'],row['allowed_employees'],row['offer_id'],
                            row['claim_before'],row['activated'],row['not_before'],row['requested_seconds'],
                            row['allocations'],now))


def list_events(store, user, after, job=None):
    import sqlite3
    if type(after) is not int or not 0<=after<=9223372036854775807:raise ControlError(400,'无效事件游标')
    if job is not None and (not isinstance(job,str) or not HEX32.fullmatch(job)):raise ControlError(400,'无效预约编号')
    uri=Path(store.path).resolve().as_uri()+'?mode=ro'
    with closing(sqlite3.connect(uri,uri=True,timeout=5)) as db:
        db.row_factory=sqlite3.Row
        rows=db.execute('''SELECT seq,job,state,offer_id,claim_before,activated,not_before,
                          requested_seconds,allocations,occurred FROM agent_queue_events e
                          WHERE e.seq>? AND (? IS NULL OR e.job=?) AND
                          (e.account=? OR EXISTS (SELECT 1 FROM json_each(e.allowed_employees) WHERE value=?))
                          ORDER BY e.seq LIMIT 100''',(after,job,job,user['id'],user['employee'])).fetchall()
        result=[]
        for row in rows:
            item=dict(row);item['allocations']=json.loads(item['allocations']);result.append(item)
        return result

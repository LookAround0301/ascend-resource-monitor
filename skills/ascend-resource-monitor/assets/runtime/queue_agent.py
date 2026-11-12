"""Idempotent, transaction-scoped A5 agent reservation creation."""
import copy
import hashlib
import json
import math
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

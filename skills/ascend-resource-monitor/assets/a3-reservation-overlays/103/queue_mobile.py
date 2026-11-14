"""Narrow mobile operations using the existing queue permissions and transaction."""
from contextlib import contextmanager
import copy
import hashlib
import json
import re
import time

from control_common import ControlError

EMPLOYEE=re.compile(r'(?:[a-z][0-9]{8}|[a-z]wx[0-9]{7}|p_[a-z][a-z0-9_]{0,31})\Z')
HEX32=re.compile(r'[0-9a-f]{32}\Z')
HEX64=re.compile(r'[0-9a-f]{64}\Z')


class TransactionStore:
    def __init__(self,database):self.database=database

    @contextmanager
    def db(self):yield self.database


def initialize(database):
    database.execute('''CREATE TABLE IF NOT EXISTS welink_command_receipts (
        operation TEXT PRIMARY KEY,employee TEXT NOT NULL,job TEXT NOT NULL,
        action TEXT NOT NULL,version TEXT NOT NULL,result TEXT NOT NULL,created REAL NOT NULL)''')


def describe(database,user,job,action):
    source=database.execute('SELECT r.*,a.employee,a.name FROM unified_tasks r JOIN accounts a ON a.id=r.account WHERE r.id=?',(job,)).fetchone()
    if source is None:raise ControlError(404,'预约已结束或不存在，请重新查询')
    row=dict(source)
    owner=row['account']==user['id']
    collaborator=user['employee'] in json.loads(row['allowed_employees'])
    if not owner and not (collaborator and action=='release' and row['state']=='active'):
        raise ControlError(403,'只能释放自己的预约，或结束你共同使用的正在使用中的预约；延后只允许申请人操作')
    if action=='release':
        if row['state']=='active':internal='finish'
        elif owner and row['state'] in ('queued','ready'):internal='cancel'
        else:raise ControlError(409,'当前预约状态不能释放，请重新查询')
        if internal=='cancel' and row['not_before'] and row['night_takeover']:
            raise ControlError(409,'夜间预约已经开始接管，请等待开始后再结束登记')
        effect=('结束使用登记，释放预约卡位。实际进程和容器不会因此停止，请自行检查；夜间接管仍按现有规则执行。'
                if internal=='finish' else '取消这条预约，释放尚未使用的预约资源。')
        neighbor=None
    else:
        if row['state'] not in ('queued','ready'):raise ControlError(409,'只有等待或待开始的预约可以延后')
        if row['urgent']:raise ControlError(409,'紧急任务不能手动顺延，请先在网页取消紧急标记')
        waiting=database.execute("SELECT id FROM unified_tasks WHERE state IN ('queued','ready') AND (not_before>0)=? AND urgent=? ORDER BY queue_order,rowid",
                                 (int(bool(row['not_before'])),row['urgent'])).fetchall()
        index=next((i for i,r in enumerate(waiting) if r['id']==job),None)
        if index is None or index+1>=len(waiting):raise ControlError(409,'已在同类型队列末位，没有可以让到前面的预约')
        neighbor=waiting[index+1]['id'];internal='priority-down'
        effect=('释放本次待开始分配，保留预约并在同类型队列向后顺延一位。' if row['state']=='ready' else
                '保留预约，在同类型队列向后顺延一位。')
        if row['not_before']:effect+='夜间开始时间可能随排程调整。'
    version=hashlib.sha256(json.dumps(dict(row=row,action=action,next_job=neighbor),sort_keys=True).encode()).hexdigest()
    return dict(job=job,version=version,state=row['state'],night=bool(row['not_before']),
                employee=row['employee'],name=row['name'],cards=row['cards']*(2 if row['dual'] else 1),
                created=row['created'],activated=row['activated'],not_before=row['not_before'],requested_seconds=row['requested_seconds'],
                allocations=json.loads(row['allocations']),fixed_node=row['fixed_node'],
                internal=internal,offer_id=row['offer_id'],effect=effect)


def command(unified,store,payload):
    if not isinstance(payload,dict) or set(payload)-{'mode','employee','job','action','version','operation','expires'}:
        raise ControlError(400,'无效手机操作')
    mode,employee,job,action=(payload.get(k) for k in ('mode','employee','job','action'))
    if (mode not in ('preview','execute') or action not in ('release','defer') or
            not isinstance(employee,str) or not EMPLOYEE.fullmatch(employee) or
            not isinstance(job,str) or not HEX32.fullmatch(job)):
        raise ControlError(400,'无效手机操作')
    now=time.time()
    with store.db() as database:
        initialize(database)
        account=database.execute('SELECT id,employee,name FROM accounts WHERE employee=?',(employee,)).fetchone()
        if account is None:raise ControlError(403,'请先在 A5 网页登录并建立个人账号')
        user=dict(account,admin=False,registered=True)
        if mode=='execute':
            operation,version,expires=(payload.get(k) for k in ('operation','version','expires'))
            if (not isinstance(operation,str) or not HEX32.fullmatch(operation) or
                    not isinstance(version,str) or not HEX64.fullmatch(version) or
                    type(expires) not in (int,float)):
                raise ControlError(400,'无效确认操作')
            old=database.execute('SELECT * FROM welink_command_receipts WHERE operation=?',(operation,)).fetchone()
            if old:
                if (old['employee'],old['job'],old['action'],old['version'])!=(employee,job,action,version):
                    raise ControlError(409,'确认操作不匹配，请重新申请')
                return json.loads(old['result'])
            if not now<expires<=now+301:raise ControlError(409,'确认已过期，请重新申请操作')
        # Work on a shallow instance with a transaction-scoped store. The shared
        # queue object and public action API are never changed or impersonated.
        queue=copy.copy(unified);queue.store=TransactionStore(database)
        queue._schedule(database,now)
        preview=describe(database,user,job,action)
        if mode=='preview':return preview
        if preview['version']!=version:raise ControlError(409,'预约状态或相邻顺位已变化，请重新查询并确认')
        schedule=queue._schedule;checked=False
        def checked_schedule(db,at):
            nonlocal checked
            result=schedule(db,at)
            if not checked:
                if describe(db,user,job,action)['version']!=version:
                    raise ControlError(409,'预约状态已变化，请重新查询并确认')
                checked=True
            return result
        queue._schedule=checked_schedule
        result=queue.action(user,job,preview['internal'],preview['offer_id'] or None)
        message=('已向后顺延一位，预约仍保留；请查询最新预计开始时间。' if action=='defer' else
                 '已结束登记，预约资源已释放。实际进程和容器不会因此停止。' if preview['state']=='active' else '已取消预约，预约资源已释放。')
        if result=='night_cleanup_pending':message='已提交结束夜间预约，正在按现有规则确认资源释放。'
        receipt=dict(message=message,job=job,action=action)
        database.execute('INSERT INTO welink_command_receipts VALUES (?,?,?,?,?,?,?)',
            (operation,employee,job,action,version,json.dumps(receipt,ensure_ascii=False),now))
        return receipt

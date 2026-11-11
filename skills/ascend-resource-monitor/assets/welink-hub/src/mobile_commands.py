"""A5 release/defer commands, proven by a short-lived code sent only in private."""
import asyncio
from contextlib import closing
import hashlib
import json
import logging
import re
import secrets
import sqlite3
import subprocess
import time

from hub_core import relevant,resources,stamp

LOG=logging.getLogger('welink-hub')


class CommandError(Exception):
    pass


def initialize(path):
    with closing(sqlite3.connect(path)) as db,db:
        db.execute('''CREATE TABLE IF NOT EXISTS mobile_challenges (
            operation TEXT PRIMARY KEY,employee TEXT NOT NULL,code_hash TEXT UNIQUE NOT NULL,
            payload TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,
            status TEXT NOT NULL,result TEXT NOT NULL DEFAULT '')''')
        db.execute('CREATE TABLE IF NOT EXISTS mobile_limits (employee TEXT,kind TEXT,at REAL)')
        db.execute('CREATE TABLE IF NOT EXISTS mobile_task_lists (employee TEXT PRIMARY KEY,jobs TEXT,expires REAL)')
        columns={r[1] for r in db.execute('PRAGMA table_info(mobile_challenges)')}
        for name,definition in [('attempts','INTEGER NOT NULL DEFAULT 0'),('next_attempt','REAL NOT NULL DEFAULT 0'),
                                ('confirmed','REAL NOT NULL DEFAULT 0'),('reply_state',"TEXT NOT NULL DEFAULT ''")]:
            if name not in columns:db.execute('ALTER TABLE mobile_challenges ADD COLUMN '+name+' '+definition)


def allowed(path,employee,kind,maximum,window,now=None):
    now=time.time() if now is None else now
    with closing(sqlite3.connect(path,timeout=5)) as db,db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM mobile_limits WHERE at<?',(now-300,))
        count=db.execute('SELECT count(*) FROM mobile_limits WHERE employee=? AND kind=? AND at>?',
                         (employee,kind,now-window)).fetchone()[0]
        if count>=maximum:return False
        db.execute('INSERT INTO mobile_limits VALUES (?,?,?)',(employee,kind,now))
        return True


def rpc(config,payload):
    settings=config['mobile_commands']
    args=['ssh','-T','-F','/dev/null','-o','BatchMode=yes','-o','IdentitiesOnly=yes',
          '-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=5',
          '-o','UserKnownHostsFile='+config['known_hosts'],'-i',settings['ssh_key'],
          'ascendwelinkcmd@'+settings['host'],'a5-mobile-command']
    result=subprocess.run(args,input=json.dumps(payload,ensure_ascii=False).encode(),
                          capture_output=True,timeout=14,check=False)
    if result.returncode or len(result.stdout)>16384:raise RuntimeError('Command transport unavailable')
    response=json.loads(result.stdout)
    if response.get('status')!=200:
        if response.get('status') in (400,403,404,409):
            raise CommandError(response.get('body',{}).get('message','预约状态已变化，请重新查询'))
        raise RuntimeError('Command outcome unconfirmed')
    return response['body']


HELP=('【A5预约 手机操作】\n/A5预约 查询\n/A5预约 释放 [序号]\n/A5预约 延后 [序号]\n'
      '释放：使用中的任务结束登记，等待或待开始的任务取消预约。\n'
      '延后：保留预约，向后顺延一位。多条预约时请先查询并指定序号。\n'
      '共同使用人可结束正在使用的预约；取消和延后仅申请人可操作。\n'
      '操作前会私聊发回任务详情；复制其中的“/A5预约 确认 确认码”才会执行，5 分钟内有效。')


class MobileCommands:
    def __init__(self,hub):
        self.hub=hub;self.path=hub.config['database'];self.lock=asyncio.Lock()
        initialize(self.path)

    async def backend(self,payload):
        return await asyncio.to_thread(rpc,self.hub.config,payload)

    def remember(self,employee,rows):
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('INSERT OR REPLACE INTO mobile_task_lists VALUES (?,?,?)',
                       (employee,json.dumps([r['id'] for r in rows]),time.time()+900))

    def rows(self,employee):
        snapshot=self.hub.snapshot('a5')
        if not snapshot:raise CommandError('A5 队列暂时不可用，请稍后重试')
        return [r for r in snapshot['tasks'] if relevant(r,employee)]

    def select(self,employee,action,selector):
        rows=self.rows(employee)
        if selector:
            if not re.fullmatch(r'[1-9][0-9]?',selector):raise CommandError('请填写查询结果中的任务序号，例如 /A5预约 释放 1')
            with closing(sqlite3.connect(self.path)) as db:
                menu=db.execute('SELECT jobs,expires FROM mobile_task_lists WHERE employee=?',(employee,)).fetchone()
            if not menu or menu[1]<time.time():
                self.remember(employee,rows)
                raise CommandError('请先发送 /A5预约 查询，查看当前任务序号后再操作')
            jobs=json.loads(menu[0]);index=int(selector)-1
            if index>=len(jobs):raise CommandError('任务序号无效，请重新查询')
            row=next((r for r in rows if r['id']==jobs[index]),None)
            if not row:raise CommandError('这条预约已结束或权限已变化，请重新查询')
            return row
        candidates=[r for r in rows if
            ((r.get('employee')==employee and r['state'] in ('queued','ready','active')) or r['state']=='active')
            and (action=='release' or (r.get('employee')==employee and r['state'] in ('queued','ready')))]
        if not candidates:raise CommandError('没有可操作的预约；共同使用人只能释放正在使用的共同预约，延后仅申请人可操作')
        if len(candidates)!=1:raise CommandError('你有多条可操作预约，请先发送 /A5预约 查询，再填写任务序号，例如 /A5预约 '+('释放' if action=='release' else '延后')+' 1')
        return candidates[0]

    async def reply(self,employee,text):
        await self.hub.send(employee,text)

    async def prepare(self,employee,action,selector):
        if not allowed(self.path,employee,'prepare',3,60):raise CommandError('操作请求较频繁，请使用上一条确认指令，或一分钟后重新申请')
        if not allowed(self.path,'*','prepare-all',20,60):raise CommandError('当前操作请求较多，请稍后再试')
        with closing(sqlite3.connect(self.path)) as db:
            unknown=db.execute("SELECT 1 FROM mobile_challenges WHERE employee=? AND status IN ('queued','executing','unknown')",(employee,)).fetchone()
        if unknown:raise CommandError('上一条操作正在后台自动核对，完成后会自动通知，请勿重复发起')
        row=self.select(employee,action,selector)
        try:preview=await self.backend(dict(mode='preview',employee=employee,job=row['id'],action=action))
        except CommandError:raise
        except Exception:raise CommandError('预约操作服务暂时不可用，请稍后重试；当前预约未被更改')
        now=time.time();operation=secrets.token_hex(16);code=secrets.token_hex(4)
        payload=dict(mode='execute',employee=employee,job=preview['job'],action=action,
                     version=preview['version'],operation=operation,expires=now+300)
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('DELETE FROM mobile_challenges WHERE expires<?',(now-86400,))
            db.execute("UPDATE mobile_challenges SET status='superseded' WHERE employee=? AND status IN ('pending','undelivered')",(employee,))
            db.execute('INSERT INTO mobile_challenges (operation,employee,code_hash,payload,created,expires,status,result) VALUES (?,?,?,?,?,?,?,?)',
                (operation,employee,hashlib.sha256(code.encode()).hexdigest(),json.dumps(payload),now,now+300,'undelivered',''))
        state={'active':'使用中','queued':'等待中','ready':'已分配，待开始'}[preview['state']]
        text=('【A5预约 待确认'+('释放' if action=='release' else '延后')+'】\n任务 '+preview['job'][:8]+' · '+('夜间任务' if preview.get('night') else '普通任务')+' · '+state+'\n'+
              '申请：'+stamp(preview.get('created'))+'；开始：'+stamp(preview.get('activated') or preview.get('not_before'))+'\n'+
              resources(preview)+'\n'+preview['effect']+'\n\n确认执行请复制发送：\n/A5预约 确认 '+code+
              '\n确认码 5 分钟内有效，只需确认一次，结果会自动反馈。')
        await self.reply(employee,text)
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute("UPDATE mobile_challenges SET status='pending' WHERE operation=? AND status='undelivered'",(operation,))

    async def confirm(self,employee,code):
        if not allowed(self.path,employee,'confirm',6,300):raise CommandError('确认尝试过多，请五分钟后重新申请操作')
        if not re.fullmatch(r'[0-9a-f]{8}',code):raise CommandError('请完整复制小鲁班私聊返回的确认指令')
        digest=hashlib.sha256(code.encode()).hexdigest();now=time.time()
        with closing(sqlite3.connect(self.path)) as db,db:
            db.row_factory=sqlite3.Row
            source=db.execute('SELECT * FROM mobile_challenges WHERE code_hash=? AND employee=?',(digest,employee)).fetchone()
            if not source or source['expires']<=now or source['status'] in ('superseded','failed'):
                raise CommandError('确认码无效、已过期或状态已变化，请重新申请操作')
            row=dict(source)
            if row['status']=='done':
                message=row['result']
            else:
                message=None
                if row['status'] in ('pending','undelivered'):
                    db.execute("UPDATE mobile_challenges SET status='queued',confirmed=?,next_attempt=0 WHERE operation=?",(now,row['operation']))
        if message:
            await self.reply(employee,'【A5预约 已执行】\n'+message);return
        await self.reply(employee,'【A5预约 已受理】\n正在处理这次操作，结果会自动通知。确认一次即可。')

    async def process_pending(self):
        now=time.time()
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory=sqlite3.Row
            rows=[dict(r) for r in db.execute("SELECT * FROM mobile_challenges WHERE status IN ('queued','unknown','executing') AND next_attempt<=? ORDER BY confirmed,created LIMIT 10",(now,))]
        for row in rows:
            with closing(sqlite3.connect(self.path)) as db,db:
                claimed=db.execute("UPDATE mobile_challenges SET status='executing',attempts=attempts+1,next_attempt=? WHERE operation=? AND status IN ('queued','unknown','executing') AND next_attempt<=?",
                                   (time.time()+30,row['operation'],time.time())).rowcount
            if not claimed:continue
            await self.process_one(row)
        await self.deliver_results()

    async def process_one(self,row):
        try:
            result=await self.backend(json.loads(row['payload']))
        except CommandError as error:
            with closing(sqlite3.connect(self.path)) as db,db:
                db.execute("UPDATE mobile_challenges SET status='failed',result=?,reply_state='pending' WHERE operation=?",
                           (str(error),row['operation']))
            return
        except Exception as error:
            delay=min(30,2**min(row['attempts']+1,5))
            with closing(sqlite3.connect(self.path)) as db,db:
                db.execute("UPDATE mobile_challenges SET status='unknown',next_attempt=? WHERE operation=?",(time.time()+delay,row['operation']))
            LOG.warning('Mobile operation response unconfirmed: %s; automatic reconciliation scheduled',type(error).__name__)
            return
        message=result['message']
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute("UPDATE mobile_challenges SET status='done',result=?,reply_state='pending' WHERE operation=?",(message,row['operation']))

    async def deliver_results(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory=sqlite3.Row
            rows=[dict(r) for r in db.execute("SELECT * FROM mobile_challenges WHERE reply_state='pending' AND status IN ('done','failed') ORDER BY created LIMIT 20")]
        for row in rows:
            with closing(sqlite3.connect(self.path)) as db,db:
                claimed=db.execute("UPDATE mobile_challenges SET reply_state='sending' WHERE operation=? AND reply_state='pending'",(row['operation'],)).rowcount
            if not claimed:continue
            try:
                await self.reply(row['employee'],('【A5预约 操作完成】\n' if row['status']=='done' else '【A5预约 操作未执行】\n')+row['result'])
            except Exception as error:
                LOG.warning('Mobile operation result delivery unconfirmed: %s',type(error).__name__)
                state='unknown'
            else:state='sent'
            with closing(sqlite3.connect(self.path)) as db,db:
                db.execute('UPDATE mobile_challenges SET reply_state=? WHERE operation=?',(state,row['operation']))

    async def handle(self,employee,params):
        async with self.lock:
            try:
                parts=params.split()
                if not parts or parts[0] not in ('释放','延后','确认') or len(parts)>2:
                    await self.reply(employee,HELP);return
                if parts[0]=='确认':
                    await self.confirm(employee,parts[1].lower() if len(parts)>1 else '')
                else:
                    await self.prepare(employee,'release' if parts[0]=='释放' else 'defer',parts[1] if len(parts)>1 else '')
            except CommandError as error:
                await self.reply(employee,'【A5预约】'+str(error))

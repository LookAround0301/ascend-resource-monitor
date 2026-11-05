"""Reservation queue and personal accounts. Never executes submitted commands."""
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

from control_common import ControlError, CONTAINER_ID


class QueueStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, employee TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL, salt TEXT NOT NULL, digest TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, account TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS queue_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reservations(id TEXT PRIMARY KEY, account TEXT NOT NULL,
                    node TEXT NOT NULL, container_id TEXT NOT NULL, container TEXT NOT NULL,
                    title TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL, activated REAL,
                    container_started_at TEXT, finished REAL, message TEXT NOT NULL DEFAULT '',
                    cancel_operation TEXT, cancel_since REAL);
                CREATE UNIQUE INDEX IF NOT EXISTS node_active ON reservations(node)
                    WHERE state IN ('active','cancelling');
                CREATE UNIQUE INDEX IF NOT EXISTS container_pending_bound ON reservations(node,container_id)
                    WHERE container_id<>'' AND state IN ('queued','active','cancelling');
                DROP INDEX IF EXISTS container_pending;
                CREATE TABLE IF NOT EXISTS confirmations(token TEXT PRIMARY KEY, account TEXT NOT NULL,
                    job TEXT NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL, expires REAL NOT NULL);
            ''')
            db.execute("INSERT OR IGNORE INTO queue_meta VALUES ('session_namespace',?)", (secrets.token_hex(16),))
            self.session_namespace=db.execute("SELECT value FROM queue_meta WHERE key='session_namespace'").fetchone()[0]
            if not re.fullmatch(r'[0-9a-f]{32}',self.session_namespace):
                raise ValueError('Invalid persisted queue session namespace')
            columns={r['name'] for r in db.execute('PRAGMA table_info(reservations)')}
            if 'cancel_since' not in columns: db.execute('ALTER TABLE reservations ADD COLUMN cancel_since REAL')
            if 'requested_seconds' not in columns:db.execute('ALTER TABLE reservations ADD COLUMN requested_seconds INTEGER NOT NULL DEFAULT 0')
            for name,default in [('auto_targets','[]'),('auto_error',''),('cancel_batch','[]'),('cancel_final_state','cancelled')]:
                if name not in columns:db.execute("ALTER TABLE reservations ADD COLUMN "+name+" TEXT NOT NULL DEFAULT '"+default+"'")
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback(); raise
        finally: db.close()

    @staticmethod
    def password(password, salt):
        if not isinstance(password, str) or not 10 <= len(password) <= 128:
            raise ControlError(400, '个人密码长度需为 10–128 个字符')
        return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32).hex()

    def register(self, employee, name, password):
        employee = employee.strip().lower() if isinstance(employee, str) else ''
        name = name.strip() if isinstance(name, str) else ''
        if not re.fullmatch(r'[a-z][0-9]{8}', employee) or not 1 <= len(name) <= 60 or any(ord(c) < 32 for c in name):
            raise ControlError(400, '请填写有效工号和姓名')
        salt = secrets.token_hex(16); digest = self.password(password, salt); account = secrets.token_hex(16)
        try:
            with self.db() as db:
                db.execute('INSERT INTO accounts VALUES(?,?,?,?,?,?)', (account, employee, name, salt, digest, time.time()))
        except sqlite3.IntegrityError: raise ControlError(409, '该工号已注册，请使用个人密码登录')
        return self.new_session(account)

    def login(self, employee, password):
        with self.db() as db: row = db.execute('SELECT * FROM accounts WHERE employee=?', (str(employee).strip().lower(),)).fetchone()
        salt = row['salt'] if row else '00' * 16
        digest = self.password(password, salt)
        if row is None or not hmac.compare_digest(row['digest'], digest): raise ControlError(401, '工号或个人密码不正确')
        return self.new_session(row['id'])

    def login_without_password(self, employee, name):
        employee = employee.strip().lower() if isinstance(employee, str) else ''
        name = name.strip() if isinstance(name, str) else ''
        if not re.fullmatch(r'[a-z][0-9]{8}', employee) or not 1 <= len(name) <= 60 or any(ord(c) < 32 for c in name):
            raise ControlError(400, '请填写有效工号和姓名')
        with self.db() as db:
            row = db.execute('SELECT id,name FROM accounts WHERE employee=?', (employee,)).fetchone()
            if row:
                if row['name'] != name:
                    raise ControlError(401, '工号与已登记姓名不一致，请核对或联系管理员')
                account = row['id']
            else:
                account = secrets.token_hex(16)
                # Retain the legacy schema and existing credentials for legacy queue deployments.
                db.execute('INSERT INTO accounts VALUES(?,?,?,?,?,?)', (account, employee, name, '', '', time.time()))
        return self.new_session(account)

    def new_session(self, account):
        token = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
            db.execute('INSERT INTO sessions VALUES(?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), account, time.time()+7*86400))
        return token

    def user(self, token):
        if not token: return None
        with self.db() as db:
            row = db.execute('SELECT a.id,a.employee,a.name FROM accounts a JOIN sessions s ON s.account=a.id WHERE s.token=? AND s.expires>?',
                             (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        return dict(row) if row else None

    def logout(self, token):
        with self.db() as db: db.execute('DELETE FROM sessions WHERE token=?', (hashlib.sha256(token.encode()).hexdigest(),))

    def view(self, user):
        account = user['id'] if user else ''
        with self.db() as db:
            rows = [dict(r) for r in db.execute('''SELECT r.*,a.employee,a.name FROM reservations r JOIN accounts a ON a.id=r.account
                WHERE r.state IN ('queued','active','cancelling') OR ((r.account=? OR ?) AND r.finished>?) ORDER BY r.created,r.id''', (account,bool(user and user.get('admin')),time.time()-7*86400))]
        busy = {r['node'] for r in rows if r['state'] in ('active','cancelling')}
        positions = {}
        for row in rows:
            row['mine'] = row.pop('account') == account
            if row['state'] == 'queued': positions[row['node']] = positions.get(row['node'],0)+1
            row['position'] = positions.get(row['node'],0) if row['state']=='queued' else None
            row['can_claim'] = row['mine'] and row['state']=='queued' and row['position']==1 and row['node'] not in busy
            row.pop('cancel_operation',None)
            row.pop('cancel_batch',None)
            row.pop('cancel_final_state',None)
            row['auto_targets']=json.loads(row['auto_targets'])
        return {'user':user, 'tasks':rows}

    def enqueue(self, user, node, cid, title, container='',requested_seconds=3600):
        if not isinstance(title,str) or not 1 <= len(title.strip()) <= 120 or any(ord(c)<32 for c in title): raise ControlError(400,'任务说明需为 1–120 个字符')
        if not isinstance(cid,str) or (cid and not CONTAINER_ID.fullmatch(cid)):raise ControlError(400,'需要完整容器 ID')
        if type(requested_seconds) is not int or not 60<=requested_seconds<=30*86400:raise ControlError(400,'预计占用时长需为 1 分钟至 30 天')
        with self.db() as db:
            if db.execute("SELECT COUNT(*) FROM reservations WHERE account=? AND state IN ('queued','active','cancelling')",(user['id'],)).fetchone()[0]>=5:
                raise ControlError(409,'每人最多保留 5 个未结束预约')
            job = secrets.token_hex(16)
            try: db.execute('INSERT INTO reservations(id,account,node,container_id,container,title,state,created,requested_seconds) VALUES(?,?,?,?,?,?,?,?,?)',
                            (job,user['id'],node,cid,container,title.strip(),'queued',time.time(),requested_seconds))
            except sqlite3.IntegrityError: raise ControlError(409,'该容器已有未结束预约')
        return job

    def own(self, user, job, db=None):
        if db is None:
            with self.db() as connection: return self.own(user,job,connection)
        row = db.execute('SELECT r.*,a.employee FROM reservations r JOIN accounts a ON a.id=r.account WHERE r.id=?',(job,)).fetchone()
        if not row: raise ControlError(404,'任务不存在')
        if row['account'] != user['id'] and not user.get('admin'): raise ControlError(403,'只能操作自己账号提交的任务')
        return dict(row)

    def active_on_node(self,user,node):
        with self.db() as db:
            row=db.execute("SELECT id FROM reservations WHERE node=? AND state='active'",(node,)).fetchone()
            if not row:raise ControlError(403,'请先在此服务器队列登记并开始使用')
            return self.own(user,row['id'],db)

    def active(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT r.*,a.employee FROM reservations r JOIN accounts a ON a.id=r.account WHERE r.state='active'")]

    def update_auto(self,job,targets=None,error=''):
        with self.db() as db:
            if targets is None:db.execute("UPDATE reservations SET auto_error=? WHERE id=? AND state='active'",(error,job))
            else:
                summary=[{k:s[k] for k in ('container_id','container_started_at','container')} for s in targets]
                db.execute("UPDATE reservations SET auto_targets=?,auto_error=? WHERE id=? AND state='active'",(json.dumps(summary),error,job))

    def batch_result(self,job,operation,state,message):
        with self.db() as db:
            row=db.execute("SELECT cancel_batch FROM reservations WHERE id=? AND state='cancelling'",(job,)).fetchone()
            if not row:return
            entries=json.loads(row[0])
            for entry in entries:
                if entry['operation_id']==operation:entry.update(state=state,message=message)
            db.execute('UPDATE reservations SET cancel_batch=? WHERE id=?',(json.dumps(entries),job))

    def active_for(self,user,node,cid):
        with self.db() as db:
            row=db.execute("SELECT * FROM reservations WHERE node=? AND container_id=? AND state='active'",(node,cid)).fetchone()
        if not row or (row['account']!=user['id'] and not user.get('admin')):
            raise ControlError(403,'请先登记自己的任务、开始使用并关联对应容器')
        return dict(row)

    def confirmation(self,user,job,action,payload):
        if not (job.startswith('direct:') and user.get('admin')): self.own(user,job)
        token = secrets.token_urlsafe(24)
        with self.db() as db:
            db.execute('DELETE FROM confirmations WHERE expires<?',(time.time(),))
            db.execute('INSERT INTO confirmations VALUES(?,?,?,?,?,?)',(token,user['id'],job,action,json.dumps(payload),time.time()+60))
        return token

    def confirm_payload(self,user,job,action,token):
        if not (job.startswith('direct:') and user.get('admin')): self.own(user,job)
        with self.db() as db:
            row=db.execute('SELECT payload FROM confirmations WHERE token=? AND account=? AND job=? AND action=? AND expires>?',
                           (token,user['id'],job,action,time.time())).fetchone()
        if not row: raise ControlError(409,'确认已失效，请重新查看进程并确认')
        return json.loads(row['payload'])

    def consume_confirmation(self,token):
        with self.db() as db:
            if db.execute('DELETE FROM confirmations WHERE token=?',(token,)).rowcount != 1:
                raise ControlError(409,'确认已使用，请刷新状态')

    def transition(self,user,job,action,token=None):
        with self.db() as db:
            row = self.own(user,job,db)
            confirmation = db.execute('SELECT * FROM confirmations WHERE token=? AND account=? AND job=? AND action=? AND expires>?',
                                      (token,user['id'],job,action,time.time())).fetchone()
            if not confirmation: raise ControlError(409,'确认已失效，请重新查看进程并确认')
            payload = json.loads(confirmation['payload'])
            if action=='cancel' and row['state']=='queued':
                db.execute("UPDATE reservations SET state='cancelled',finished=? WHERE id=?",(time.time(),job))
                db.execute('DELETE FROM confirmations WHERE token=?',(token,))
                return row,None
            if action=='claim':
                head = db.execute("SELECT id FROM reservations WHERE node=? AND state='queued' ORDER BY created,id LIMIT 1",(row['node'],)).fetchone()
                busy = db.execute("SELECT 1 FROM reservations WHERE node=? AND state IN ('active','cancelling')",(row['node'],)).fetchone()
                if row['state']!='queued' or busy or not head or head['id']!=job: raise ControlError(409,'还没有轮到该预约')
                db.execute("UPDATE reservations SET state='active',activated=?,container_started_at=?,message='' WHERE id=?",(time.time(),payload['container_started_at'],job))
            elif action=='bind':
                if row['state']!='active' or row['container_id']:raise ControlError(409,'只能为使用中且未关联容器的任务添加关联')
                cid=payload['container_id']
                if not CONTAINER_ID.fullmatch(cid):raise ControlError(400,'需要完整容器 ID')
                try:db.execute('UPDATE reservations SET container_id=?,container=?,container_started_at=? WHERE id=?',
                               (cid,payload['container'],payload['container_started_at'],job))
                except sqlite3.IntegrityError:raise ControlError(409,'该容器已关联其他未结束的任务')
            elif action in ('cancel','finish'):
                if row['state']!='active': raise ControlError(409,'任务当前不可执行此操作')
                finish_stops=action=='finish' and payload.get('finish_stop') is True and (
                    bool(payload['processes']) or (payload.get('mode')=='container'
                    and bool(payload.get('container_id')) and not payload.get('container_changed')))
                final_state='completed' if action=='finish' else 'cancelled'
                if action=='finish' and not finish_stops:
                    if payload['processes']: raise ControlError(409,'仍有 NPU 进程，请选择停止方式并重新确认结束登记')
                    db.execute("UPDATE reservations SET state='completed',finished=? WHERE id=?",(time.time(),job))
                    payload=None
                elif payload.get('targets'):
                    payload['operations']=[{'operation_id':secrets.token_hex(16),'snapshot':target} for target in payload['targets']]
                    batch=[{'operation_id':op['operation_id'],'state':'pending','message':''} for op in payload['operations']]
                    db.execute("UPDATE reservations SET state='cancelling',cancel_batch=?,cancel_since=?,cancel_final_state=?,message='' WHERE id=?",(json.dumps(batch),time.time(),final_state,job))
                elif 'targets' in payload or not row['container_id']:
                    db.execute("UPDATE reservations SET state='cancelled',finished=? WHERE id=?",(time.time(),job))
                    payload=None
                else:
                    operation=secrets.token_hex(16);payload['operation_id']=operation
                    db.execute("UPDATE reservations SET state='cancelling',cancel_operation=?,cancel_batch='[]',cancel_since=?,cancel_final_state=?,message='' WHERE id=?",(operation,time.time(),final_state,job))
            else: raise ControlError(400,'无效任务操作')
            db.execute('DELETE FROM confirmations WHERE token=?',(token,))
            return row,payload

    def resolve_cancel(self,job,success,message):
        with self.db() as db:
            db.execute("""UPDATE reservations SET state=CASE WHEN ? THEN cancel_final_state ELSE 'active' END,
                       message=CASE WHEN ? AND cancel_final_state='completed' THEN '已确认停止成功，结束登记已完成，队列已释放' ELSE ? END,
                       finished=? WHERE id=? AND state='cancelling'""",
                       (bool(success),bool(success),message,time.time() if success else None,job))

    def cancelling(self):
        with self.db() as db: return [dict(r) for r in db.execute("SELECT r.*,a.employee FROM reservations r JOIN accounts a ON a.id=r.account WHERE r.state='cancelling'")]

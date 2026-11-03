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
                CREATE TABLE IF NOT EXISTS grants(node TEXT NOT NULL, container_id TEXT NOT NULL,
                    container TEXT NOT NULL, account TEXT NOT NULL, PRIMARY KEY(node,container_id));
                CREATE TABLE IF NOT EXISTS reservations(id TEXT PRIMARY KEY, account TEXT NOT NULL,
                    node TEXT NOT NULL, container_id TEXT NOT NULL, container TEXT NOT NULL,
                    title TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL, activated REAL,
                    container_started_at TEXT, finished REAL, message TEXT NOT NULL DEFAULT '',
                    cancel_operation TEXT, cancel_since REAL);
                CREATE UNIQUE INDEX IF NOT EXISTS node_active ON reservations(node)
                    WHERE state IN ('active','cancelling');
                CREATE UNIQUE INDEX IF NOT EXISTS container_pending ON reservations(node,container_id)
                    WHERE state IN ('queued','active','cancelling');
                CREATE TABLE IF NOT EXISTS confirmations(token TEXT PRIMARY KEY, account TEXT NOT NULL,
                    job TEXT NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL, expires REAL NOT NULL);
            ''')
            columns={r['name'] for r in db.execute('PRAGMA table_info(reservations)')}
            if 'cancel_since' not in columns: db.execute('ALTER TABLE reservations ADD COLUMN cancel_since REAL')
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

    def admin_data(self):
        with self.db() as db:
            return {'accounts': [dict(r) for r in db.execute('SELECT id,employee,name FROM accounts ORDER BY employee')],
                    'grants': [dict(r) for r in db.execute('SELECT * FROM grants ORDER BY node,container')]}

    def grant(self, node, cid, container, account):
        if not CONTAINER_ID.fullmatch(cid): raise ControlError(400, '需要完整容器 ID')
        with self.db() as db:
            if db.execute("SELECT 1 FROM reservations WHERE node=? AND container_id=? AND state IN ('queued','active','cancelling')", (node,cid)).fetchone():
                raise ControlError(409, '该容器仍有预约，请先由预约人结束或取消任务')
            if account is None: db.execute('DELETE FROM grants WHERE node=? AND container_id=?', (node,cid))
            else:
                if not db.execute('SELECT 1 FROM accounts WHERE id=?', (account,)).fetchone(): raise ControlError(400, '账号不存在')
                db.execute('INSERT OR REPLACE INTO grants VALUES(?,?,?,?)', (node,cid,container,account))

    def view(self, user):
        account = user['id'] if user else ''
        with self.db() as db:
            rows = [dict(r) for r in db.execute('''SELECT r.*,a.employee,a.name FROM reservations r JOIN accounts a ON a.id=r.account
                WHERE r.state IN ('queued','active','cancelling') OR (r.account=? AND r.finished>?) ORDER BY r.created,r.id''', (account,time.time()-7*86400))]
            grants = [dict(r) for r in db.execute('SELECT * FROM grants WHERE account=? ORDER BY node,container', (account,))]
        busy = {r['node'] for r in rows if r['state'] in ('active','cancelling')}
        positions = {}
        for row in rows:
            row['mine'] = row.pop('account') == account
            if row['state'] == 'queued': positions[row['node']] = positions.get(row['node'],0)+1
            row['position'] = positions.get(row['node'],0) if row['state']=='queued' else None
            row['can_claim'] = row['mine'] and row['state']=='queued' and row['position']==1 and row['node'] not in busy
            row.pop('cancel_operation',None)
        return {'user':user, 'tasks':rows, 'grants':grants}

    def enqueue(self, user, node, cid, title):
        if not isinstance(title,str) or not 1 <= len(title.strip()) <= 120 or any(ord(c)<32 for c in title): raise ControlError(400,'任务说明需为 1–120 个字符')
        with self.db() as db:
            grant = db.execute('SELECT * FROM grants WHERE node=? AND container_id=? AND account=?',(node,cid,user['id'])).fetchone()
            if not grant: raise ControlError(403,'请先由管理员将该容器授权给你的账号')
            if db.execute("SELECT COUNT(*) FROM reservations WHERE account=? AND state IN ('queued','active','cancelling')",(user['id'],)).fetchone()[0]>=5:
                raise ControlError(409,'每人最多保留 5 个未结束预约')
            job = secrets.token_hex(16)
            try: db.execute('INSERT INTO reservations(id,account,node,container_id,container,title,state,created) VALUES(?,?,?,?,?,?,?,?)',
                            (job,user['id'],node,cid,grant['container'],title.strip(),'queued',time.time()))
            except sqlite3.IntegrityError: raise ControlError(409,'该容器已有未结束预约')
        return job

    def own(self, user, job, db=None):
        if db is None:
            with self.db() as connection: return self.own(user,job,connection)
        row = db.execute('SELECT * FROM reservations WHERE id=?',(job,)).fetchone()
        if not row: raise ControlError(404,'任务不存在')
        if row['account'] != user['id'] and not user.get('admin'): raise ControlError(403,'只能操作自己账号提交的任务')
        return dict(row)

    def active_for(self,user,node,cid):
        with self.db() as db:
            row=db.execute("SELECT * FROM reservations WHERE node=? AND container_id=? AND state='active'",(node,cid)).fetchone()
        if not row or (row['account']!=user['id'] and not user.get('admin')):
            raise ControlError(403,'请先在任务队列中为自己的授权容器登记并开始使用')
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
                if not db.execute('SELECT 1 FROM grants WHERE node=? AND container_id=? AND account=?',(row['node'],row['container_id'],row['account'])).fetchone():
                    raise ControlError(403,'容器授权已变更')
                db.execute("UPDATE reservations SET state='active',activated=?,container_started_at=?,message='' WHERE id=?",(time.time(),payload['container_started_at'],job))
            elif action in ('cancel','finish'):
                if row['state']!='active': raise ControlError(409,'任务当前不可执行此操作')
                if action=='finish':
                    if payload['processes']: raise ControlError(409,'仍有 NPU 进程，请先自行停止或使用取消任务')
                    db.execute("UPDATE reservations SET state='completed',finished=? WHERE id=?",(time.time(),job))
                else:
                    operation=secrets.token_hex(16);payload['operation_id']=operation
                    db.execute("UPDATE reservations SET state='cancelling',cancel_operation=?,cancel_since=?,message='' WHERE id=?",(operation,time.time(),job))
            else: raise ControlError(400,'无效任务操作')
            db.execute('DELETE FROM confirmations WHERE token=?',(token,))
            return row,payload

    def resolve_cancel(self,job,success,message):
        with self.db() as db:
            db.execute("UPDATE reservations SET state=?,message=?,finished=? WHERE id=? AND state='cancelling'",
                       ('cancelled' if success else 'active',message,time.time() if success else None,job))

    def cancelling(self):
        with self.db() as db: return [dict(r) for r in db.execute("SELECT * FROM reservations WHERE state='cancelling'")]

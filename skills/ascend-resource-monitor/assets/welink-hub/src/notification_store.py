"""Durable notification episodes, repeat intervals, and a bounded-lived outbox."""
from contextlib import closing
import json
import sqlite3
import time

BLOCKING={'overdue','overdue-joint','overdue-dependent'}


def init_notices(path):
    with closing(sqlite3.connect(path)) as db,db:
        db.execute('''CREATE TABLE IF NOT EXISTS notice_state (
            cluster TEXT,recipient TEXT,scope TEXT,episode INTEGER,sequence INTEGER,
            live INTEGER,last_kind TEXT,last_attempt REAL,last_seen REAL,
            PRIMARY KEY(cluster,recipient,scope))''')
        db.execute('CREATE TABLE IF NOT EXISTS notice_sources (cluster TEXT PRIMARY KEY,initialized REAL)')
        db.execute('''CREATE TABLE IF NOT EXISTS notice_outbox (
            event TEXT PRIMARY KEY,cluster TEXT,recipient TEXT,payload TEXT,
            expires REAL,status TEXT,created REAL)''')


def reserve_notices(path,snapshot,recipients,enabled_at,now=None,legacy_employee=None):
    now=time.time() if now is None else now
    cluster=snapshot['cluster']
    if (snapshot.get('notifications_version')!=1 or not
            -30<=now-snapshot.get('notifications_generated_at',0)<=90):
        return
    live={}
    for event in snapshot['notifications']:
        if event['recipient'] not in recipients or event['valid_until']<=now:
            continue
        scope=event.get('scope') or event['key']
        live[(event['recipient'],scope)]=event
    with closing(sqlite3.connect(path,timeout=5)) as db,db:
        db.row_factory=sqlite3.Row
        db.execute('BEGIN IMMEDIATE')
        first=not db.execute('SELECT 1 FROM notice_sources WHERE cluster=?',(cluster,)).fetchone()
        db.execute('INSERT OR IGNORE INTO notice_sources VALUES (?,?)',(cluster,now))
        previous={(r['recipient'],r['scope']):dict(r) for r in db.execute(
            'SELECT * FROM notice_state WHERE cluster=?',(cluster,))}
        for (recipient,scope),event in sorted(live.items()):
            old=previous.get((recipient,scope))
            episode=(old['episode'] if old and old['live'] else old['episode']+1 if old else 1)
            last=old['last_attempt'] if old and old['live'] else 0
            sequence=old['sequence'] if old and old['live'] else 0
            kind=old['last_kind'] if old and old['live'] else ''
            repeated=max(0,float(event.get('repeat_seconds') or 0))
            escalated=bool(kind=='overdue-nonblocking' and event['kind'] in BLOCKING)
            historical=(bool(first and event.get('historical')) or
                        bool(event.get('occurred_at') and event['occurred_at']<enabled_at))
            # Recognize the allocation already notified by the previous worker.
            legacy=False
            if event['kind']=='ready' and event.get('offer_id'):
                keys=['ready:%s:%s:%s:%s'%(cluster,event['job'],event['offer_id'],recipient)]
                if cluster=='a5' and legacy_employee and recipient==legacy_employee:
                    keys.append('ready:%s:%s'%(event['job'],event['offer_id']))
                legacy=any(db.execute('SELECT 1 FROM deliveries WHERE event=?',(key,)).fetchone() for key in keys)
            eligible=not last or bool(repeated and now-last>=repeated) or escalated
            if historical or legacy:
                last=last or now
                kind=event['kind']
            elif eligible:
                sequence+=1
                delivery='notice:%s:%s:%s:%s:%s'%(cluster,recipient,scope,episode,sequence)
                payload=dict(event)
                payload['_episode']=episode
                if escalated:
                    payload['title']=('超时任务共同阻塞' if event['kind']=='overdue-joint' else
                                      '超时任务阻塞后续预约' if event['kind']=='overdue-dependent' else '超时任务已转为瓶颈')
                    payload['body']=('你的超时预约与其他超时任务共同影响后续预约，请尽快处理。' if event['kind']=='overdue-joint' else
                                     '你的超时预约占用后续预约所需资源，请尽快处理。' if event['kind']=='overdue-dependent' else
                                     '你的超时预约已由非瓶颈转为瓶颈，正在阻挡后续预约，请尽快处理。')
                db.execute('INSERT OR IGNORE INTO notice_outbox VALUES (?,?,?,?,?,?,?)',
                           (delivery,cluster,recipient,json.dumps(payload,ensure_ascii=False),event['valid_until'],'queued',now))
                last=now
                kind=event['kind']
            db.execute('INSERT OR REPLACE INTO notice_state VALUES (?,?,?,?,?,?,?,?,?)',
                       (cluster,recipient,scope,episode,sequence,1,kind,last,now))
        for (recipient,scope),old in previous.items():
            if (recipient,scope) not in live and old['live']:
                db.execute('UPDATE notice_state SET live=0 WHERE cluster=? AND recipient=? AND scope=?',
                           (cluster,recipient,scope))


def queued(path,now=None):
    now=time.time() if now is None else now
    with closing(sqlite3.connect(path,timeout=5)) as db,db:
        db.execute("UPDATE notice_outbox SET status='expired' WHERE status='queued' AND expires<=?",(now,))
        return [dict(event=r[0],cluster=r[1],recipient=r[2],payload=json.loads(r[3])) for r in db.execute(
            "SELECT event,cluster,recipient,payload FROM notice_outbox WHERE status='queued' AND expires>? ORDER BY created,event LIMIT 200",(now,))]


def begin_attempt(path,event,now=None):
    now=time.time() if now is None else now
    with closing(sqlite3.connect(path,timeout=5)) as db,db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute("SELECT cluster,recipient,payload FROM notice_outbox WHERE event=? AND status='queued'",(event,)).fetchone()
        if not row:
            return False
        payload=json.loads(row[2])
        state=db.execute('SELECT live,episode FROM notice_state WHERE cluster=? AND recipient=? AND scope=?',
                         (row[0],row[1],payload.get('scope') or payload['key'])).fetchone()
        if not state or not state[0] or state[1]!=payload['_episode']:
            db.execute("UPDATE notice_outbox SET status='cancelled' WHERE event=?",(event,))
            return False
        # Commit before the HTTP call; unknown outcomes are not blindly retried.
        return bool(db.execute("UPDATE notice_outbox SET status='attempted' WHERE event=? AND status='queued' AND expires>?",(event,now)).rowcount)


def accepted(path,event):
    with closing(sqlite3.connect(path,timeout=5)) as db,db:
        db.execute("UPDATE notice_outbox SET status='accepted' WHERE event=? AND status='attempted'",(event,))


def rejected(path,event):
    with closing(sqlite3.connect(path,timeout=5)) as db,db:
        db.execute("UPDATE notice_outbox SET status='rejected' WHERE event=? AND status='attempted'",(event,))

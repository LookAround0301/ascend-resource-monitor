"""Persist two distinct observations and a per-person/node group cooldown."""
from contextlib import closing
import html
import hashlib
import math
import re
import sqlite3
import time


def initialize(path):
    with closing(sqlite3.connect(path)) as db,db:
        db.execute('''CREATE TABLE IF NOT EXISTS group_observations (
            scope TEXT PRIMARY KEY,event TEXT,first_sample REAL,last_sample REAL,
            samples INTEGER,active INTEGER,last_attempt REAL,channel TEXT NOT NULL DEFAULT '')''')
        if 'channel' not in {r[1] for r in db.execute('PRAGMA table_info(group_observations)')}:
            db.execute("ALTER TABLE group_observations ADD COLUMN channel TEXT NOT NULL DEFAULT ''")
        if 'node' not in {r[1] for r in db.execute('PRAGMA table_info(group_observations)')}:
            db.execute("ALTER TABLE group_observations ADD COLUMN node TEXT NOT NULL DEFAULT ''")


def reserve(path,snapshot,config,now=None):
    now=time.time() if now is None else now
    if (not config.get('enabled') or not config.get('mention_verified') or
            not re.fullmatch(r'[0-9]{10,30}',str(config.get('group','')))
            or snapshot.get('cluster')!=config.get('cluster','a5')
            or snapshot.get('notifications_version')!=1
            or not -30<=now-snapshot.get('notifications_generated_at',0)<=90):return []
    result=[];present=set()
    channel=hashlib.sha256((snapshot['cluster']+':'+str(config['group'])).encode()).hexdigest()
    with closing(sqlite3.connect(path,timeout=5)) as db,db:
        db.row_factory=sqlite3.Row;db.execute('BEGIN IMMEDIATE')
        for event in snapshot.get('violations',[]):
            sample=event['sampled_at']
            # Source inventory and feed timestamps share A5's clock. Compare
            # them there, then separately enforce the feed's age on this hub.
            # Comparing the inventory directly with the hub clock can reject
            # valid samples whenever the two machines differ by a few seconds.
            if (not isinstance(sample,(int,float)) or not math.isfinite(sample)
                    or not -15<=snapshot['notifications_generated_at']-sample<=30):continue
            scope=hashlib.sha256(('%s:%s:%s:%s'%(snapshot['cluster'],config['group'],event['node'],event['employee'])).encode()).hexdigest()
            present.add(scope)
            old=db.execute('SELECT * FROM group_observations WHERE scope=?',(scope,)).fetchone()
            continuous=bool(old and old['active'] and old['event']==event['id'] and 0<=sample-old['last_sample']<=90)
            first=old['first_sample'] if continuous else sample
            count=old['samples']+int(sample>old['last_sample']) if continuous else 1
            last=old['last_attempt'] if old else 0
            if count>=2 and sample-first>=10 and (not last or now-last>=max(60,config.get('repeat_seconds',600))):
                last=now
                key='group:'+scope+':'+str(int(now*1000))
                db.execute('INSERT INTO deliveries VALUES (?,?,?)',(key,now,'attempted'))
                result.append(dict(event=key,group=str(config['group']),violation=event))
            db.execute('INSERT OR REPLACE INTO group_observations (scope,event,first_sample,last_sample,samples,active,last_attempt,channel,node) VALUES (?,?,?,?,?,?,?,?,?)',
                       (scope,event['id'],first,sample,count,1,last,channel,event['node']))
        fresh=set(snapshot.get('fresh_nodes',snapshot.get('nodes',[])))
        for row in db.execute('SELECT scope,node FROM group_observations WHERE active=1 AND channel=?',(channel,)).fetchall():
            if row['scope'] not in present and row['node'] in fresh:
                db.execute('UPDATE group_observations SET active=0 WHERE scope=?',(row['scope'],))
        for event in snapshot.get('extensions',[]):
            if not -30<=now-event['at']<=86400:continue
            key='group-extension:'+channel+':'+event['id']
            if db.execute('INSERT OR IGNORE INTO deliveries VALUES (?,?,?)',(key,now,'attempted')).rowcount:
                result.append(dict(event=key,group=str(config['group']),extension=event))
    return result


def message(snapshot,event):
    return ('【'+snapshot['label']+' 预约卡占用提醒】\n@'+html.escape(event['name'])+'（'+event['employee']+'）\n'+
            event['node']+' 的 '+ '、'.join(map(str,event['cards']))+' 号卡检测到你的进程，当前不在你的预约授权范围内，且已由其他用户预约。'
            '\n请核对并释放这些卡位，或与当前预约人协调登记共同使用。')


def extension_message(snapshot,event):
    allocation='；'.join(node+'：'+'、'.join(map(str,cards))+' 号卡' for node,cards in event['allocations'].items())
    return ('【'+snapshot['label']+' 延长使用通告】\n'+html.escape(event['name'].replace('@','＠'))+'（'+event['employee']+'）的预约已延长至少 '
            +str(event['count'])+' 次，本次 '+str(event['minutes'])+' 分钟，累计至少 '+str(event['total'])+' 分钟。\n'+allocation)

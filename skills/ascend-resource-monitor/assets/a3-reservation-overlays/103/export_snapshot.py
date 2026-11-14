"""Export a minimal queue snapshot; never write to the queue database."""
import json
from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.request import ProxyHandler, Request, build_opener

EMPLOYEE = re.compile(r'(?:[a-z][0-9]{8}|[a-z]wx[0-9]{7}|p_[a-z][a-z0-9_]{0,31})')
FIELDS = ('id', 'employee', 'name', 'state', 'created', 'activated',
          'requested_seconds', 'effective_requested_seconds', 'cards', 'dual',
          'fixed_node', 'total_cards', 'position', 'not_before', 'estimated_start',
          'allocations', 'planned_allocations', 'offer_id', 'claim_before',
          'finish_before', 'offer_end_before', 'night_requested_start',
          'night_schedule_blocked', 'overdue_seconds', 'extension_minutes')
NOTICE_FIELDS = ('recipient','key','scope','kind','title','body','job','repeat_seconds',
                 'valid_until','occurred_at','historical','offer_id','phase')


def export(config):
    with build_opener(ProxyHandler({})).open(config['state_url'], timeout=8) as response:
        state = json.loads(response.read(4_000_001))
    if not isinstance(state, dict) or not isinstance(state.get('tasks'), list):
        raise ValueError('Invalid queue state')
    with closing(sqlite3.connect(Path(config['database']).as_uri() + '?mode=ro', uri=True,
                                timeout=5)) as database:
        accounts = sorted({employee.lower() for employee, in database.execute(
            'SELECT employee FROM accounts') if EMPLOYEE.fullmatch(employee.lower())})
    tasks = []
    for row in state['tasks']:
        if row.get('state') not in ('queued', 'ready', 'active'):
            continue
        item = {key: row[key] for key in FIELDS if key in row}
        item['allowed_people'] = [
            {'employee': p['employee'], 'name': p.get('name', '')}
            for p in row.get('allowed_people', []) if isinstance(p, dict)
            and isinstance(p.get('employee'), str) and EMPLOYEE.fullmatch(p['employee'])]
        tasks.append(item)
    snapshot = dict(version=1, cluster=config['cluster'], label=config['label'],
                    exported_at=time.time(), server_time=state.get('server_time'),
                    nodes=state.get('nodes', []), accounts=accounts, tasks=tasks)
    if config.get('notifications_url'):
        token=Path(config['notification_token_file']).read_text(encoding='utf-8').strip()
        request=Request(config['notifications_url'],headers={'Authorization':'Bearer '+token})
        with build_opener(ProxyHandler({})).open(request,timeout=8) as response:
            feed=json.loads(response.read(4_000_001))
        if (feed.get('version')!=1 or not isinstance(feed.get('events'),list)
                or len(feed['events'])>10000 or not -30<=time.time()-feed['generated_at']<=90
                or not all(isinstance(p,str) and EMPLOYEE.fullmatch(p) for p in feed['recipients'])):
            raise ValueError('Invalid notification feed')
        notices=[]
        recipients=set(feed['recipients'])
        for event in feed['events']:
            if (event.get('recipient') not in recipients or not isinstance(event.get('key'),str)
                    or len(event['key'])>600 or not isinstance(event.get('body'),str)
                    or len(event['body'])>5000 or not isinstance(event.get('title'),str)
                    or len(event['title'])>200):
                raise ValueError('Invalid notification event')
            notices.append({key:event[key] for key in NOTICE_FIELDS if key in event})
        snapshot.update(notifications_version=1,notifications=notices,
                        notifications_generated_at=feed['generated_at'],notification_recipients=sorted(recipients))
        violations=[]
        for event in feed.get('violations',[]):
            if (not isinstance(event,dict) or not EMPLOYEE.fullmatch(event.get('employee',''))
                    or not isinstance(event.get('name'),str) or not 1<=len(event['name'])<=100
                    or any(ord(c)<32 for c in event['name']) or event.get('node') not in state.get('nodes',[])
                    or not isinstance(event.get('cards'),list) or not event['cards']
                    or any(type(card) is not int or not 0<=card<state.get('cards_per_node',8) for card in event['cards'])
                    or not re.fullmatch(r'[0-9a-f]{24}',event.get('id',''))):
                raise ValueError('Invalid group candidate')
            violations.append({k:event[k] for k in ['id','node','employee','name','cards','sampled_at']})
        if len(violations)>1000:raise ValueError('Too many group candidates')
        snapshot['violations']=violations
        fresh_nodes=feed.get('fresh_nodes',[])
        if not isinstance(fresh_nodes,list) or any(n not in state.get('nodes',[]) for n in fresh_nodes):
            raise ValueError('Invalid inventory freshness')
        snapshot['fresh_nodes']=fresh_nodes
        extensions=[]
        for event in feed.get('extensions',[]):
            if (not isinstance(event,dict) or not re.fullmatch(r'extension:[A-Za-z0-9_-]{1,100}:[0-9]+',event.get('id',''))
                    or not EMPLOYEE.fullmatch(event.get('employee','')) or not isinstance(event.get('name'),str)
                    or not 1<=len(event['name'])<=100 or any(ord(c)<32 for c in event['name'])
                    or type(event.get('count')) is not int or event['count']<2
                    or type(event.get('minutes')) is not int or event['minutes'] not in (10,20,30)
                    or type(event.get('total')) is not int or event['total']<event['minutes']
                    or not isinstance(event.get('at'),(int,float)) or not -30<=time.time()-event['at']<=86400
                    or not isinstance(event.get('allocations'),dict)
                    or any(node not in state.get('nodes',[]) or not isinstance(cards,list)
                        or any(type(card) is not int or not 0<=card<state.get('cards_per_node',8) for card in cards)
                        for node,cards in event['allocations'].items())):
                raise ValueError('Invalid group extension event')
            extensions.append({k:event[k] for k in ('id','employee','name','count','minutes','total','at','allocations')})
        if len(extensions)>1000:raise ValueError('Too many group extension events')
        snapshot['extensions']=extensions
    target = Path(config['output'])
    temporary = target.with_suffix('.tmp')
    # Fixed, private directory; the queue service is its only writer.
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640),
                   'w', encoding='utf-8') as stream:
        json.dump(snapshot, stream, ensure_ascii=False, separators=(',', ':'))
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o640)
    os.replace(temporary, target)
    print('Snapshot refreshed; tasks=%d accounts=%d' % (len(tasks), len(accounts)))


if __name__ == '__main__':
    try:
        export(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')))
    except Exception as error:
        # No queue contents, filesystem details or credentials in journal.
        print('Snapshot refresh failed: ' + type(error).__name__, file=sys.stderr)
        sys.exit(1)

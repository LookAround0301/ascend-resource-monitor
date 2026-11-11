"""Private, read-only export of all queue desktop notification conditions."""
import ipaddress
import json
import math
import re
import secrets

EMPLOYEE=re.compile(r'[a-z][0-9]{8}\Z')
ALERT_TITLES={
    'unregistered':'插队提醒','over-allocation':'预约资源占用提醒','reset':'预约清空通知',
    'night-rescheduled':'夜间预约顺延提醒','extended-next':'排队延长提醒',
    'overtime-release':'预约登记已释放','overdue':'使用超时提醒',
    'overdue-joint':'使用超时提醒','overdue-dependent':'使用超时提醒',
    'overdue-nonblocking':'使用超时提醒','overdue-next':'使用超时提醒',
    'overdue-joint-next':'使用超时提醒','overdue-dependent-next':'使用超时提醒'}


def authorize_export(remote,authorization,token):
    try:
        local=ipaddress.ip_address(remote).is_loopback
    except (ValueError,TypeError):
        return False
    return local and len(token)>=40 and secrets.compare_digest(authorization,'Bearer '+token)


def resource(allocation):
    return '；'.join(node+'：'+'、'.join(map(str,cards)) for node,cards in allocation.items()) or '待分配'


def task_events(row,now,state):
    job=row['id'];result=[]
    def add(key,kind,title,body,**extra):
        result.append(dict(key='task:'+job+':'+key,kind=kind,title=title,body=body,
                           job=job,valid_until=now+90,**extra))
    # The browser prioritizes a new deferral before the current state notice.
    # Separate keys let the relay retain both rather than lose one between polls.
    if row.get('missed_claims'):
        suffix=('；累计 '+str(state['missed_claim_limit'])+' 次未开始将自动取消' if state.get('missed_claim_limit') else '')
        add('deferred:'+str(row['missed_claims']),'deferred','预约已顺延一位',
            '超过 10 分钟未开始，这次分配已释放，预约向后顺延一位'+suffix+'。请查看新的排队状态。',historical=True)
    if row['state']=='ready' and row.get('can_claim'):
        remaining=max(0,row['claim_before']-now)
        if remaining<=0:
            return result
        elapsed=max(0,now-(row.get('ready_since') or now))
        phase='last-minute' if remaining<=60 else str(math.floor(elapsed/60))
        offer=str(row.get('offer_id') or row.get('ready_since') or row['claim_before'])
        body=resource(row['allocations'])+'，剩余 '+str(math.ceil(remaining/60))+' 分钟内开始。'
        body+=('短时插空当前最多可用 '+str(math.floor(row.get('effective_requested_seconds',0)/60))+' 分钟，请按时自行停止任务。'
               if row.get('backfill_planned') else '')
        if state.get('missed_claim_limit'):
            body+='累计 '+str(state['missed_claim_limit'])+' 次分配后 10 分钟未开始将自动取消预约。'
        elif not row.get('backfill_planned'):
            body+='超时未开始预约将向后顺延一位。'
        if state.get('overtime_control'):
            body+='登记释放不会停止实际进程，请先查看卡使用情况。'
        add('ready:'+offer+':'+phase,'ready' if phase=='0' else 'ready-reminder',
            '资源已分配' if phase=='0' else '提醒：请及时开始使用',body,offer_id=offer,phase=phase)
        result[-1]['valid_until']=min(now+90,row['claim_before'])
    elif row['state']=='active' and row.get('activated') and row.get('requested_seconds'):
        remaining=row['activated']+row['requested_seconds']-now
        elapsed=now-row['activated']
        if 0<=elapsed<30:
            add('started:'+str(row['activated']),'started','已开始使用',resource(row['allocations'])+'。'+
                ('当前是瓶颈任务，预计结束时间会影响等待中的预约，请按时释放。' if row.get('bottleneck') else '请留意预计结束时间。'),
                occurred_at=row['activated'])
        elif remaining>0:
            if row.get('backfill_planned') and remaining<=300 and row['requested_seconds']>300:
                add('backfill-ending:5','backfill-ending','插空任务即将结束',
                    '请在剩余 '+str(math.ceil(remaining/60))+' 分钟内自行停止任务并结束登记，以免影响前序预约。')
            elif not row.get('not_before') and row.get('bottleneck'):
                threshold=5 if remaining<=300 and row['requested_seconds']>300 else 10 if 300<remaining<=600 and row['requested_seconds']>600 else 0
                if threshold:
                    add('bottleneck-ending:'+str(threshold),'bottleneck-ending','瓶颈任务即将到期',
                        '预计剩余 '+str(math.ceil(remaining/60))+' 分钟；预计结束时间会影响等待中的预约。')
    return result


def release_alert(release,unified):
    prefix=('夜间预约已到上午 '+f'{int(unified.night_carryover_cutoff_hour):02d}:{int(unified.night_carryover_cutoff_hour*60)%60:02d} 截止时间' if release['reason']=='night-cutoff'
            else '已到凌晨 1 点截止时间' if release['reason']=='cutoff' else '预约超时超过 10 分钟')
    suffix=('登记释放本身不会停止容器；如果预约卡进入夜间接管，将按夜间规则清理。'
            if unified.normal_priority else '容器和实际任务不会自动停止，请自行检查并停止任务。')
    return dict(id='overtime-release:'+release['job'],kind='overtime-release',
                message=prefix+'，登记已自动释放；'+suffix,occurred_at=release['released_at'])


def build_feed(unified,store,monitor):
    context={}
    base=unified.view(None,notification_context=context)
    now=base['server_time']
    with store.db() as database:
        people={r['employee']:dict(r) for r in database.execute('SELECT id,employee,name FROM accounts')
                if EMPLOYEE.fullmatch(r['employee'])}
        history=[dict(r) for r in database.execute('SELECT * FROM unified_notice_events WHERE at>? AND at<=?',(now-86400,now))]
    owners={row['id']:row['account'] for row in context['rows']}
    allowed={row['id']:set(json.loads(row['allowed_employees'])) for row in context['rows']}
    # A named collaborator may not yet have opened/logged into the A5 webpage.
    for employee in set().union(*allowed.values()) if allowed else ():
        if EMPLOYEE.fullmatch(employee):
            people.setdefault(employee,dict(id='',employee=employee,name=''))
    continuing=unified.continuing()
    releases=[r for r in unified.released_usage() if now-86400<r['released_at']<=now] if unified.overtime_control else []
    for release in releases:
        for employee in [release['employee'],*release.get('allowed_employees',[])]:
            if EMPLOYEE.fullmatch(employee):
                people.setdefault(employee,dict(id='',employee=employee,name=''))
    for record in history:
        for employee in [record['employee'],*json.loads(record['allowed_employees'])]:
            if EMPLOYEE.fullmatch(employee):
                people.setdefault(employee,dict(id='',employee=employee,name=''))
    events=[]
    for employee,person in sorted(people.items()):
        user=dict(person,registered=True,admin=False)
        rows=[]
        for original in base['tasks']:
            row=dict(original)
            row['mine']=bool(user['id'] and owners[row['id']]==user['id'])
            row['collaborator']=employee in allowed[row['id']]
            row['allowed_employees']=sorted(allowed[row['id']])
            row['can_claim']=(row['mine'] or row['collaborator']) and row['state']=='ready'
            rows.append(row)
        alerts=unified.alerts(context['rows'],user,now,context['reset'],
                              context['blocked'],context['dependent'],context['joint'])
        # Existing next-in-line alert selection matches account IDs. Include the
        # same alert for each explicitly registered collaborator of that waiter.
        for row in rows:
            if not row['collaborator'] or row['mine'] or row['state']!='queued':
                continue
            owner=dict(id=owners[row['id']],employee=row['employee'])
            for alert in unified.alerts(context['rows'],owner,now,None,
                    context['blocked'],context['dependent'],context['joint']):
                if alert['id'].endswith(':next:'+row['id']) or alert['id'].endswith(':dependent-next:'+row['id']):
                    alerts.append(alert)
        for release in releases:
            if release['account']==user['id'] or employee==release['employee'] or employee in release.get('allowed_employees',[]):
                alerts.append(release_alert(release,unified))
        state=dict(base,user=user,tasks=rows,alerts=alerts,overtime_control=unified.overtime_control)
        if monitor:
            # Co-users own the same reservation cards for notification purposes.
            monitor_rows=[dict(row,mine=row['mine'] or row['collaborator']) for row in rows]
            alerts=monitor.decorate(dict(state,tasks=monitor_rows),continuing,releases)['alerts']
        unique={}
        for alert in alerts:
            event=dict(recipient=employee,key='alert:'+alert['id'],kind=alert['kind'],
                title=ALERT_TITLES.get(alert['kind'],'使用超时提醒'),body=alert['message'],
                job=alert.get('job',''),repeat_seconds=alert.get('repeat_seconds',0),valid_until=now+90)
            if alert.get('job') and alert.get('repeat_seconds'):
                event['scope']='overdue:'+alert['job']
            if alert['kind']=='overtime-release':
                event['occurred_at']=alert['occurred_at']
            elif alert['kind']=='night-rescheduled' and alert['id'].startswith('night-rescheduled:'):
                event['occurred_at']=float(alert['id'].rsplit(':',1)[1])
            elif alert['kind']=='extended-next':
                event['occurred_at']=float(alert['id'].rsplit(':',1)[1])
            unique[event['key']]=event
        for row in rows:
            if row['mine'] or row['collaborator']:
                for event in task_events(row,now,state):
                    event['recipient']=employee
                    unique[event['key']]=event
        for record in history:
            if record['kind']=='missed-cancel' and employee in [record['employee'],*json.loads(record['allowed_employees'])]:
                event=dict(recipient=employee,key=record['id'],kind='missed-cancel',
                    title='预约已自动取消',body='资源分配后累计 '+str(json.loads(record['payload']).get('limit',3))+' 次超过 10 分钟未登记开始，该预约已取消；如仍需使用，请重新预约。',
                    job=record['job'],occurred_at=record['at'],valid_until=record['at']+86400)
                unique[event['key']]=event
        events.extend(unique.values())
    from queue_group_alerts import violations
    extensions=[dict(id=r['id'],employee=r['employee'],name=r['name'],at=r['at'],**json.loads(r['payload']))
                for r in history if r['kind']=='extension']
    return dict(version=1,generated_at=now,recipients=sorted(people),events=events,
                violations=violations(monitor,base['tasks'],continuing,releases,now),extensions=extensions,
                fresh_nodes=sorted(monitor.fresh_nodes(now)) if monitor else [])

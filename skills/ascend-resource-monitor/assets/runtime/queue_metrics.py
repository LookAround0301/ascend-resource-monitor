"""Public queue display metrics. Never emits credentials or session identifiers."""
from datetime import datetime,timezone as dt_timezone
import re
from zoneinfo import ZoneInfo


def render_queue_metrics(rows,nodes,now,prefix='ascend',timezone='UTC'):
    if not re.fullmatch('[a-zA-Z_][a-zA-Z0-9_]*',prefix):raise ValueError('Invalid metric prefix')
    tz=dt_timezone.utc if timezone=='UTC' else ZoneInfo(timezone)
    def stamp(value):return datetime.fromtimestamp(value,tz).strftime('%Y-%m-%d %H:%M:%S') if value else '—'
    def escape(value):return str(value).replace('\\','\\\\').replace('\n','\\n').replace('"','\\"')
    name=prefix+'_queue_task_usage_seconds'
    output=['# HELP '+name+' Elapsed time since a task was claimed; waiting and empty queues are zero.',
            '# TYPE '+name+' gauge']
    for node in nodes:
        tasks=[r for r in rows if r['node']==node and r['state'] in ('queued','active','cancelling')]
        if not tasks:tasks=[{'id':'','state':'empty','title':'暂无排队或使用中的任务'}]
        for row in tasks:
            seconds=row.get('requested_seconds',0)
            duration=(str(round(seconds/3600,2))+' 小时') if seconds else '—'
            elapsed=max(0,now-row['activated']) if row.get('activated') and row['state'] in ('active','cancelling') else 0
            labels={'node':node,'task_id':row['id'],'employee_id':row.get('employee','—'),'employee_name':row.get('name','—'),
                    'task_title':row['title'],'queue_position':row.get('position') or 0,
                    'queue_status':{'queued':'排队中','active':'使用中','cancelling':'正在结束','empty':'当前空闲'}[row['state']],
                    'requested_at':stamp(row.get('created')),'requested_duration':duration,
                    'started_at':stamp(row.get('activated')),'container':row.get('container') or '—'}
            output.append(name+'{'+','.join(key+'="'+escape(value)+'"' for key,value in labels.items())+'} '+str(round(elapsed,3)))
    output += ['# TYPE '+prefix+'_queue_source_up gauge',prefix+'_queue_source_up 1']
    return '\n'.join(output)+'\n'

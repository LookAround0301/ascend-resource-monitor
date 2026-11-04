"""Read-only comparison of fresh NPU process identities with active reservations."""
import hashlib
import json
import math
import re
import time
from datetime import datetime
from urllib.parse import urlsplit

EMPLOYEE = re.compile(r'[a-z][0-9]{8}\Z')


class QueueMonitor:
    def __init__(self, nodes, settings, fetch):
        self.nodes=list(nodes); self.fetch=fetch; self.rows=[]; self.valid=set()
        self.sampled=0; self.next_poll=0;self.inventory_times={}
        self.prefix=settings.get('metric_prefix','ascend')
        self.process_prefix=settings.get('process_metric_prefix',self.prefix+'_process_')
        if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*',self.prefix) or not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*',self.process_prefix):
            raise ValueError('Invalid monitoring metric prefix')
        self.url=settings.get('prometheus_url','http://127.0.0.1:9090').rstrip('/')
        parsed=urlsplit(self.url)
        if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost','::1') or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Queue process monitoring requires a loopback Prometheus URL')

    @property
    def query(self):
        names='|'.join(self.process_prefix+x for x in ('inventory_success','inventory_truncated','inventory_timestamp_seconds','container_metadata_age_seconds','inventory_processes','start_time_seconds'))
        return '{__name__=~"'+names+'"}'

    async def poll(self):
        if time.monotonic()<self.next_poll:
            return
        self.next_poll=time.monotonic()+10
        try:
            data=await self.fetch(self.url,self.query)
            self.ingest(data,time.time())
        except Exception:
            self.rows=[]; self.valid=set();self.inventory_times={}; self.sampled=time.time()

    def ingest(self, data, now):
        if data.get('status')!='success' or data.get('data',{}).get('resultType')!='vector':
            raise ValueError('Unavailable process inventory')
        scalars={node:{} for node in self.nodes}; rows={node:[] for node in self.nodes}
        for item in data['data']['result']:
            labels=item['metric']; node=labels.get('node'); name=labels.get('__name__','')
            if node not in rows or not name.startswith(self.process_prefix):
                continue
            value=float(item['value'][1])
            if not math.isfinite(value):raise ValueError('Invalid inventory value')
            suffix=name[len(self.process_prefix):]
            if suffix=='start_time_seconds':
                key=labels.get('process_key','');pid=labels.get('pid','')
                if not key or not pid.isdigit():raise ValueError('Missing process identity')
                rows[node].append({'node':node,'process_key':key,'pid':pid,
                                  'employee':labels.get('employee_id','').strip().lower(),
                                  'name':labels.get('employee_name','').strip(),
                                  'container':labels.get('container',''),
                                  'control_ref':labels.get('control_ref','')})
            else:
                scalars[node][suffix]=value
        valid=set()
        for node,values in scalars.items():
            if (values.get('inventory_success')==1 and values.get('inventory_truncated')==0
                    and 0<=now-values.get('inventory_timestamp_seconds',0)<=30
                    and 0<=values.get('container_metadata_age_seconds',1e9)<=45
                    and values.get('inventory_processes')==len(rows[node])
                    and len({r['process_key'] for r in rows[node]})==len(rows[node])):
                valid.add(node)
        self.rows=[r for node in valid for r in rows[node]]; self.valid=valid; self.sampled=now
        self.inventory_times={node:scalars[node]['inventory_timestamp_seconds'] for node in valid}

    def fresh_nodes(self,now):
        return {node for node in self.valid if 0<=now-self.sampled<=20 and 0<=now-self.inventory_times.get(node,0)<=30}

    def current(self, now):
        valid=self.fresh_nodes(now)
        return [r for r in self.rows if r['node'] in valid]

    def classification(self,row, tasks, continuing=()):
        if not EMPLOYEE.fullmatch(row['employee']) or not row['name'] or len(row['name'])>100 or any(ord(c)<32 for c in row['name']):
            return '待核实'
        matched=any(task['state']=='active' and task['employee']==row['employee']
                    and row['node'] in task['allocations'] for task in tasks)
        if matched:return '已登记使用'
        cid,_,started=row.get('control_ref','').partition('@')
        if not cid or not started:return '未登记使用（疑似插队）'
        try:start=datetime.fromisoformat(started.replace('Z','+00:00')).timestamp()
        except ValueError:return '待核实'
        if any(g['node']==row['node'] and g['employee']==row['employee'] and start<=g['cutoff'] for g in continuing):
            return '跨日继续使用'
        return '未登记使用（疑似插队）'

    def decorate(self, state, continuing=()):
        now=state['server_time']; grouped={}
        for row in self.current(now):
            if self.classification(row,state['tasks'],continuing)=='未登记使用（疑似插队）':
                grouped.setdefault((row['node'],row['employee'],row['name']),[]).append(row['process_key'])
        alerts=list(state.get('alerts',[]))
        for (node,employee,name),keys in sorted(grouped.items()):
            digest=hashlib.sha256(('\0'.join(sorted(keys))).encode()).hexdigest()[:20]
            alerts.append({'id':'unregistered:'+node+':'+employee+':'+digest,'kind':'unregistered',
                           'message':'未登记使用提醒（疑似插队）：'+name+'（'+employee+'）在 '+node+' 有 NPU 进程，未匹配该机当前使用登记，请核对并登记。'})
        valid=self.fresh_nodes(now)
        return {**state,'alerts':alerts,'process_monitor_errors':[node+' 的进程采集暂不可用，暂停插队判断' for node in self.nodes if node not in valid]}

    def metrics(self, tasks, now=None, continuing=()):
        now=time.time() if now is None else now
        metric=self.prefix+'_queue_process_registration'
        lines=['# HELP '+metric+' Current NPU process registration, joined by node and process_key.', '# TYPE '+metric+' gauge']
        for row in self.current(now):
            labels={'node':row['node'],'process_key':row['process_key'],'queue_registration':self.classification(row,tasks,continuing)}
            lines.append(metric+'{'+','.join(k+'='+json.dumps(v,ensure_ascii=False) for k,v in labels.items())+'} 1')
        lines.append('# TYPE '+self.prefix+'_queue_process_source_up gauge')
        for node in self.nodes:
            lines.append(self.prefix+'_queue_process_source_up{node='+json.dumps(node)+'} '+str(int(node in self.fresh_nodes(now))))
        return '\n'.join(lines)+'\n'

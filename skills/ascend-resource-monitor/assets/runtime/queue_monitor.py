"""Read-only comparison of fresh NPU process identities with active reservations."""
import hashlib
import json
import math
import re
import time
from datetime import datetime
from urllib.parse import urlsplit

EMPLOYEE = re.compile(r'[a-z][0-9]{8}\Z')
MAX_FUTURE_SKEW_SECONDS = 15


class QueueMonitor:
    def __init__(self, nodes, settings, fetch):
        self.nodes=list(nodes); self.fetch=fetch; self.rows=[]; self.valid=set()
        self.sampled=0; self.next_poll=0;self.inventory_times={};self.night_empty={}
        self.prefix=settings.get('metric_prefix','ascend')
        self.process_prefix=settings.get('process_metric_prefix',self.prefix+'_process_')
        self.capacity=settings.get('cards_per_node',8)
        if type(self.capacity) is not int or self.capacity not in (8,16):
            raise ValueError('cards_per_node must be 8 or 16')
        self.npu_card_map=settings.get('npu_card_map')
        if self.npu_card_map is not None:
            if (not isinstance(self.npu_card_map,dict) or set(self.npu_card_map)!=set(self.nodes)
                    or any(not isinstance(mapping,dict) or not mapping or any(
                        not isinstance(chip,str) or not re.fullmatch(r'[0-9]+',chip)
                        or type(card) is not int or not 0<=card<self.capacity
                        for chip,card in mapping.items()) for mapping in self.npu_card_map.values())):
                raise ValueError('Invalid per-node NPU chip-to-card mapping')

        if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*',self.prefix) or not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*',self.process_prefix):
            raise ValueError('Invalid monitoring metric prefix')
        self.url=settings.get('prometheus_url','http://127.0.0.1:9090').rstrip('/')
        parsed=urlsplit(self.url)
        if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost','::1') or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Queue process monitoring requires a loopback Prometheus URL')

    def physical_npu_ids(self,node,value):
        if self.npu_card_map is None:return value
        mapping=self.npu_card_map[node]
        chips=value.split(',')
        if not chips or any(chip not in mapping for chip in chips):return ''
        return ','.join(str(card) for card in sorted({mapping[chip] for chip in chips}))

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
                                  'npu_ids':self.physical_npu_ids(node,labels.get('npu_ids','')),
                                  'control_ref':labels.get('control_ref','')})
            else:
                scalars[node][suffix]=value
        valid=set()
        for node,values in scalars.items():
            if (values.get('inventory_success')==1 and values.get('inventory_truncated')==0
                    and -MAX_FUTURE_SKEW_SECONDS<=now-values.get('inventory_timestamp_seconds',0)<=30
                    and 0<=values.get('container_metadata_age_seconds',1e9)<=45
                    and values.get('inventory_processes')==len(rows[node])
                    and len({r['process_key'] for r in rows[node]})==len(rows[node])):
                valid.add(node)
        self.rows=[r for node in valid for r in rows[node]]; self.valid=valid; self.sampled=now
        self.inventory_times={node:scalars[node]['inventory_timestamp_seconds'] for node in valid}

    def fresh_nodes(self,now):
        return {node for node in self.valid if 0<=now-self.sampled<=20 and -MAX_FUTURE_SKEW_SECONDS<=now-self.inventory_times.get(node,0)<=30}

    def current(self, now):
        valid=self.fresh_nodes(now)
        return [r for r in self.rows if r['node'] in valid]

    def night_completion_decisions(self, tasks, now):
        active={task['id'] for task in tasks}
        for job in set(self.night_empty)-active:
            del self.night_empty[job]
        observed,finished=set(),set()
        valid=self.fresh_nodes(now)
        rows=self.current(now)
        for task in tasks:
            job=task['id'];allocations=task['allocations'];nodes=set(allocations)
            if (not isinstance(task['activated'],(int,float)) or not task['activated'] or task['requested_seconds']<=0 or
                    not nodes or not nodes <= valid or any(node not in self.nodes or not cards or
                    any(type(card) is not int or not 0<=card<self.capacity for card in cards)
                    for node,cards in allocations.items())):
                self.night_empty.pop(job,None);continue
            assigned={node:set(cards) for node,cards in allocations.items()}
            relevant=[row for row in rows if row['node'] in nodes]
            if any(self.cards(row) is None for row in relevant):
                self.night_empty.pop(job,None);continue
            overlapping=[row for row in relevant if self.cards(row) & assigned[row['node']]]
            authorized={task['employee'],*task['allowed_employees']}
            if any(row['employee'] in authorized for row in overlapping):observed.add(job)
            if overlapping or not (task['night_seen_usage'] or now >= task['activated']+task['requested_seconds']):
                self.night_empty.pop(job,None);continue
            item=self.night_empty.setdefault(job,{'since':now,'samples':{node:set() for node in nodes}})
            if now<item['since'] or set(item['samples'])!=nodes:
                self.night_empty.pop(job,None);continue
            for node in nodes:item['samples'][node].add(self.inventory_times[node])
            if now-item['since']>=20 and all(len(samples)>=3 for samples in item['samples'].values()):
                finished.add(job);self.night_empty.pop(job,None)
        return observed,finished

    def cards(self,row):
        raw=row.get('npu_ids','').split(',')
        return {int(card) for card in raw} if raw and all(re.fullmatch(r'0|[1-9][0-9]?',card) and int(card)<self.capacity for card in raw) else None

    @staticmethod
    def shared_cards(row, tasks, continuing=()):
        cards=set()
        for task in tasks:
            if task['state']=='active' and row['employee'] in task.get('allowed_employees',[]):
                cards.update(task['allocations'].get(row['node'],[]))
        _,_,started=row.get('control_ref','').partition('@')
        try:start=datetime.fromisoformat(started.replace('Z','+00:00')).timestamp()
        except ValueError:return cards
        for grant in continuing:
            if 'cards' in grant and grant['node']==row['node'] and grant['employee']==row['employee'] and start<=grant['cutoff']:
                cards.update(grant['cards'])
        return cards

    def classification(self,row, tasks, continuing=()):
        if not EMPLOYEE.fullmatch(row['employee']) or not row['name'] or len(row['name'])>100 or any(ord(c)<32 for c in row['name']):
            return '待核实'
        matched=any(task['state']=='active' and task['employee']==row['employee']
                    and row['node'] in task['allocations'] for task in tasks)
        if matched:return '已登记使用'
        shared=self.shared_cards(row,tasks)
        process_cards=self.cards(row)
        if shared and process_cards is None:return '待核实'
        if shared and process_cards and process_cards <= shared:return '已登记使用'
        cid,_,started=row.get('control_ref','').partition('@')
        if not cid or not started:return '未登记使用（疑似插队）'
        try:start=datetime.fromisoformat(started.replace('Z','+00:00')).timestamp()
        except ValueError:return '待核实'
        shared_total=self.shared_cards(row,tasks,continuing)
        if process_cards and shared_total and process_cards <= shared_total:
            return '跨日继续使用'
        for grant in continuing:
            if grant['node']!=row['node'] or grant['employee']!=row['employee'] or start>grant['cutoff']:
                continue
            if 'cards' not in grant:return '跨日继续使用'
            if process_cards is None:return '待核实'
            if process_cards <= set(grant['cards']):return '跨日继续使用'
        return '未登记使用（疑似插队）'

    def decorate(self, state, continuing=()):
        now=state['server_time']; grouped={}; excess_grouped={}
        owned={}; active_owned={}
        if state.get('user'):
            for task in state['tasks']:
                if task.get('mine') and task['state'] in ('active','ready'):
                    for node,cards in task['allocations'].items():
                        owned.setdefault(node,set()).update(cards)
                        if task['state']=='active':active_owned.setdefault(node,set()).update(cards)
        for row in self.current(now):
            classification=self.classification(row,state['tasks'],continuing)
            if classification=='已登记使用' and state.get('user'):
                cards=self.cards(row)
                if cards is not None:
                    permitted=set(self.shared_cards(row,state['tasks'],continuing))
                    for task in state['tasks']:
                        if task['state']=='active' and task['employee']==row['employee']:
                            permitted.update(task['allocations'].get(row['node'],[]))
                    overlap=(cards-permitted) & active_owned.get(row['node'],set())
                    if overlap:
                        group=excess_grouped.setdefault((row['node'],row['employee'],row['name']),{'keys':[],'cards':set()})
                        group['keys'].append(row['process_key']);group['cards'].update(overlap)
            if classification=='未登记使用（疑似插队）':
                cards=self.cards(row)
                if cards is None:
                    continue
                cards-=self.shared_cards(row,state['tasks'],continuing)
                overlap=cards & owned.get(row['node'],set())
                own=bool(state.get('user') and state['user'].get('employee','').lower()==row['employee'])
                for role,matched in (('affected',overlap),('self',cards if own else set())):
                    if matched:
                        group=grouped.setdefault((row['node'],row['employee'],row['name'],role),{'keys':[],'cards':set()})
                        group['keys'].append(row['process_key']);group['cards'].update(matched)
        alerts=list(state.get('alerts',[]))
        for (node,employee,name,role),group in sorted(grouped.items()):
            cards=sorted(group['cards'])
            digest=hashlib.sha256(('\0'.join(sorted(group['keys']))+':'+','.join(map(str,cards))).encode()).hexdigest()[:20]
            description = ('你在 '+node+' 的 '+ '、'.join(map(str,cards))+' 号卡有未登记进程，请核对并登记。'
                           if role=='self' else name+'（'+employee+'）在 '+node+' 的 '+ '、'.join(map(str,cards))+' 号卡有未登记进程，占用了你的预约资源，请核对。')
            alerts.append({'id':'unregistered:'+role+':'+node+':'+employee+':'+digest,'kind':'unregistered',
                           'message':'未登记使用提醒（疑似插队）：'+description})
        for (node,employee,name),group in sorted(excess_grouped.items()):
            cards=sorted(group['cards'])
            digest=hashlib.sha256(('\0'.join(sorted(group['keys']))+':'+','.join(map(str,cards))).encode()).hexdigest()[:20]
            alerts.append({'id':'over-allocation:'+node+':'+employee+':'+digest,'kind':'over-allocation',
                           'message':name+'（'+employee+'）在 '+node+' 的 '+ '、'.join(map(str,cards))+' 号卡使用超出其预约范围，占用了你的预约资源，请核对。'})
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

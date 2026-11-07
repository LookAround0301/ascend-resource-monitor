"""Revalidate a single container before an explicitly configured night kill."""
import re
from control_common import ControlError,validate_request


class NightEnforcer:
    def __init__(self,host):
        self.host=host

    def stop(self,request,actor):
        if (not isinstance(request,dict) or set(request)!={'node','container_id','container_started_at','confirmed','allowed','expected'}
                or not isinstance(actor,dict) or actor.get('id')!='night-scheduler'):
            raise ControlError(400,'无效夜间接管请求')
        identity={key:request[key] for key in ('node','container_id','container_started_at','confirmed')}
        validate_request(identity)
        if identity['node']!=self.host.engine.config['node']:
            raise ControlError(400,'目标主机不匹配')
        capacity=self.host.engine.config.get('task_queue',{}).get('cards_per_node',8)
        if capacity not in (8,16):raise ControlError(503,'夜间卡位配置无效')
        allowed=request['allowed'];expected=request['expected']
        if (not isinstance(allowed,dict) or not allowed or len(allowed)>16
                or any(not re.fullmatch(r'0|[1-9][0-9]?',str(card)) or int(card)>=capacity
                       or not isinstance(people,list) or any(not isinstance(p,str) or not p for p in people)
                       for card,people in allowed.items())
                or not isinstance(expected,list) or not expected or len(expected)>200
                or len(expected)!=len(set(expected)) or any(not isinstance(x,str) or not x for x in expected)):
            raise ControlError(400,'无效夜间卡位或进程身份')
        if not self.host.ownership:
            raise ControlError(503,'夜间进程归属核验不可用')
        scope={int(card):set(people) for card,people in allowed.items()}
        cid=identity['container_id'];started=identity['container_started_at']
        engine=self.host.engine
        with engine.lock:
            snapshot=self.host.snapshot(cid,started)
            if snapshot.get('container_changed') or not snapshot.get('inventory_complete'):
                raise ControlError(409,'容器或 NPU 进程已变化，夜间接管需重新核验')
            rows=self.host.ownership.reader()
            members={row['pid']:row for row in rows if row['container_id']==cid}
            observed=set();authorized=False;unauthorized=False
            for process in snapshot['processes']:
                row=members.get(process['pid'])
                if (not row or row['started_at']!=started or not self.host.ownership.identity_check(row)
                        or not row['process_key'].endswith(':'+str(process['start_ticks']))):
                    raise ControlError(409,'进程身份已变化，夜间接管需重新核验')
                employee=row.get('employee','');raw=row.get('npu_ids','')
                if not employee or employee in ('未知','待确认') or not raw or any(not re.fullmatch(r'0|[1-9][0-9]?',v) for v in raw.split(',')):
                    raise ControlError(409,'进程归属或卡号不明，未停止容器')
                if any(int(v)>=capacity for v in raw.split(',')):
                    raise ControlError(409,'进程卡号超出配置范围，未停止容器')
                if set(map(int,raw.split(',')))-scope.keys():
                    raise ControlError(409,'容器还使用夜间预约范围外的卡，未停止容器')
                overlap=set(map(int,raw.split(','))) & scope.keys()
                if not overlap:continue
                observed.add(row['process_key'])
                authorized |= any(employee in scope[card] for card in overlap)
                unauthorized |= any(employee not in scope[card] for card in overlap)
            if observed!=set(expected) or not unauthorized or authorized:
                raise ControlError(409,'预约授权或进程已变化，未停止容器')
            event={'action':'queue.night.kill','node':identity['node'],'container_id':cid,
                   'container_started_at':started,'process_keys':sorted(observed),
                   'cards':sorted(scope),'actor':actor}
            engine.write_audit({**event,'result':'requested'})
            try:
                engine.docker.kill(cid)
                try:info=engine.docker.inspect(cid)
                except ControlError as exc:
                    if exc.status!=404:raise
                    info={'State':{'Running':False}}
                if info.get('State',{}).get('Running'):
                    raise ControlError(409,'容器仍在运行，夜间接管未完成')
            except Exception:
                engine.write_audit({**event,'result':'uncertain'})
                raise
            engine.write_audit({**event,'result':'stopped'})
            return {'stopped':True}

"""Match current NPU containers to the operator-requested employee convention."""
import json
import time
import urllib.request
from container_guard import EMPLOYEE,LABEL,current_process
from control_common import CONTAINER_ID,ControlError


def parse_inventory(raw,prefix,now):
    scalars={};rows=[]
    for line in raw.splitlines():
        if line.startswith(prefix) and '{' not in line:
            key,value=line.split();scalars[key[len(prefix):]]=float(value)
        elif line.startswith(prefix+'start_time_seconds{'):
            labels={k:json.loads(v) for k,v in LABEL.findall(line)}
            target=labels.get('control_ref','').split('@',1)
            rows.append({'pid':int(labels['pid']),'employee':labels.get('employee_id','').strip().lower(),
                         'process_key':labels.get('process_key',''),
                         'container_id':target[0] if len(target)==2 and CONTAINER_ID.fullmatch(target[0]) else '',
                         'started_at':target[1] if len(target)==2 else ''})
    if (scalars.get('inventory_success')!=1 or scalars.get('inventory_truncated')!=0
            or not 0<=now-scalars.get('inventory_timestamp_seconds',0)<=30
            or not 0<=scalars.get('container_metadata_age_seconds',1e9)<=45
            or scalars.get('inventory_processes')!=len(rows)):
        raise ControlError(503,'工号采集数据不完整或已过期，暂不执行自动匹配')
    return rows


class QueueOwnership:
    def __init__(self,host,reader=None,identity_check=None):
        self.host=host;self.reader=reader or self.read;self.identity_check=identity_check or current_process

    def read(self):
        cfg=self.host.engine.config['task_queue']
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(cfg['process_url'],timeout=3) as response:raw=response.read(8*1024*1024+1)
            if len(raw)>8*1024*1024:raise ValueError('inventory limit')
            return parse_inventory(raw.decode(),cfg.get('process_metric_prefix','ascend_process_'),time.time())
        except ControlError:raise
        except Exception as e:raise ControlError(503,'工号采集暂不可用，未执行自动匹配') from e

    def owned(self,employee):
        if not isinstance(employee,str) or not EMPLOYEE.fullmatch(employee):raise ControlError(400,'工号格式无效')
        rows=self.reader();groups={}
        for row in rows:
            if row['container_id']:groups.setdefault(row['container_id'],[]).append(row)
        targets=[];blocked=0
        for cid,members in sorted(groups.items()):
            if not any(row['employee']==employee for row in members):continue
            try:snapshot=self.host.snapshot(cid)
            except ControlError as e:
                if e.status in (404,409):continue
                if e.status==403:blocked+=1;continue
                raise
            if not snapshot['processes']:continue
            if not snapshot.get('inventory_complete',True):blocked+=1;continue
            by_pid={r['pid']:r for r in members}
            valid=True
            for process in snapshot['processes']:
                row=by_pid.get(process['pid'])
                if (not row or row['employee']!=employee or row['started_at']!=snapshot['container_started_at']
                        or not self.identity_check(row)
                        or not row['process_key'].endswith(':'+str(process['start_ticks']))):
                    valid=False;break
            if not valid:blocked+=1;continue
            snapshot['owner_employee']=employee;targets.append(snapshot)
        if len(targets)>32:raise ControlError(503,'匹配容器过多，请管理员检查')
        return {'targets':targets,'blocked':blocked}

    def verify(self,snapshot,actor):
        if actor.get('admin') is True:return
        employee=actor.get('employee')
        if not employee or snapshot.get('owner_employee')!=employee:raise ControlError(403,'目标工号与登录账号不一致')
        matches=self.owned(employee)
        target=next((s for s in matches['targets'] if s['container_id']==snapshot['container_id']),None)
        if not target or not self.host.same(snapshot,target):raise ControlError(409,'工号归属或进程已变化，请刷新后重新确认')

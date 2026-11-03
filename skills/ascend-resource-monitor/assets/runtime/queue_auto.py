"""Employee auto-association and durable multi-container cancellation."""
import json
import time
from urllib.parse import urlencode
from control_common import ControlError


class AutoQueue:
    def __init__(self,store,agent):self.store=store;self.agent=agent

    async def fetch(self,row,strict=True):
        result=await self.agent(row['node'],'/queue/owned?'+urlencode({'employee':row['employee']}))
        error='存在工号混合或无法核实的容器，请管理员检查' if result['blocked'] else ''
        self.store.update_auto(row['id'],result['targets'],error)
        if strict and error:raise ControlError(409,error)
        return {'targets':result['targets'],'processes':[p for s in result['targets'] for p in s['processes']],
                'container_id':'','container_started_at':None,'auto_match':True,'blocked':result['blocked']}

    async def refresh(self):
        for row in self.store.active():
            try:await self.fetch(row,strict=False)
            except Exception:self.store.update_auto(row['id'],error='工号匹配暂不可用，Stop 前需重新核实')

    async def submit(self,row,payload,actor,mode):
        for operation in payload['operations']:
            try:await self.agent(row['node'],'/queue/kill',{'request':{**operation,'mode':mode},'actor':actor})
            except ControlError as e:self.store.batch_result(row['id'],operation['operation_id'],'failed',e.message)
            except Exception:pass  # Uncertain delivery: reconcile the durable operation, never retry signals.

    async def reconcile(self,row):
        entries=json.loads(row['cancel_batch'])
        for entry in entries:
            if entry['state']!='pending':continue
            try:result=await self.agent(row['node'],'/queue/operation?operation_id='+entry['operation_id'])
            except ControlError as e:
                if e.status!=404 or time.time()-(row.get('cancel_since') or time.time())<=120:continue
                result={'state':'failed','message':'主机未登记请求，未自动重试'}
            except Exception:continue
            if result['state']!='pending':
                entry.update(result);self.store.batch_result(row['id'],entry['operation_id'],result['state'],result['message'])
        if any(e['state']=='pending' for e in entries):return
        if any(e['state']!='succeeded' for e in entries):
            self.store.resolve_cancel(row['id'],False,'部分目标停止失败或未确认，任务保留；请刷新后检查')
            return
        try:remaining=await self.fetch(row,strict=False)
        except Exception:return
        if remaining['processes'] or remaining['blocked']:
            self.store.resolve_cancel(row['id'],False,'仍检测到同工号 NPU 作业或归属冲突，任务保留')
        else:self.store.resolve_cancel(row['id'],True,'已确认的目标均已停止，任务已结束')

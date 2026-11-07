"""Coordinate fixed night handoffs against fresh node-agent evidence."""
import asyncio
import time
from queue_night_policy import enforcement_decisions


class NightCoordinator:
    def __init__(self,queue,monitor,agent,audit):
        self.queue=queue;self.monitor=monitor;self.agent=agent;self.audit=audit
        self.status=[];self.attempted={}

    def decisions(self,now):
        tasks=self.queue.due_night_tasks(now)
        rows=[{**row,'cards':self.monitor.cards(row)} for row in self.monitor.current(now)]
        targets,unknown=enforcement_decisions(tasks,rows,self.monitor.fresh_nodes(now),now)
        return tasks,targets,unknown

    async def poll(self):
        now=time.time()
        if getattr(self.queue,'normal_priority',False):
            await asyncio.to_thread(self.queue.lock_due_night_takeovers,now)
        tasks,targets,unknown=self.decisions(now)
        if not tasks:
            self.status=[];return
        blocked_nodes={item['node'] for item in unknown}
        for task in tasks:
            end=(task.get('activated') or task['not_before'])+task['requested_seconds']
            if task.get('night_end_requested'):end=min(end,task['night_end_requested'])
            boundary=end if now>=end else task['not_before']
            for node in task['allocations']:
                if self.monitor.inventory_times.get(node,0)<boundary:
                    blocked_nodes.add(node)
        blocked_cards={}
        for item in targets:blocked_cards.setdefault(item['node'],set()).update(item['cards'])
        self.status=[]
        if blocked_nodes:self.status.append('夜间资源接管待核实：进程采集、归属或共享容器存在不确定情况。')
        if targets:self.status.append('夜间资源接管正在清理非预约容器，确认释放后开始登记。')
        for target in targets:
            if target['node'] in blocked_nodes:continue
            # A cancellation or whitelist change must invalidate a stale action.
            _,current,problems=self.decisions(time.time())
            if any(item['node']==target['node'] for item in problems):continue
            current=next((item for item in current if item['node']==target['node']
                          and item['control_ref']==target['control_ref']),None)
            if current!=target:continue
            key=(target['node'],target['control_ref'],tuple(target['process_keys']))
            sample=self.monitor.inventory_times.get(target['node'])
            if self.attempted.get(key)==sample:continue
            self.attempted[key]=sample
            cid,started=target['control_ref'].split('@',1)
            request={'node':target['node'],'container_id':cid,'container_started_at':started,'confirmed':True,
                     'allowed':target['allowed'],'expected':target['process_keys']}
            actor={'id':'night-scheduler','login':'night-scheduler'}
            try:
                await asyncio.wait_for(self.agent(target['node'],'/queue/night-stop',{'request':request,'actor':actor}),timeout=8)
                self.audit({'action':'queue.night.takeover','node':target['node'],'container_id':cid,
                            'cards':target['cards'],'result':'stopped'})
            except Exception:
                self.status.append('夜间容器停止未确认，请管理员核实节点与容器状态。')
                try:self.audit({'action':'queue.night.takeover','node':target['node'],'container_id':cid,
                                'cards':target['cards'],'result':'unconfirmed'})
                except Exception:pass
        # Use the pre-stop snapshot: a subsequent fresh sample must prove the
        # process disappeared before registration or resource release changes.
        await asyncio.to_thread(self.queue.finish_night_takeovers,tasks,blocked_nodes,blocked_cards,now)
        active={(item['node'],item['control_ref'],tuple(item['process_keys'])) for item in targets}
        self.attempted={key:sample for key,sample in self.attempted.items() if key in active}

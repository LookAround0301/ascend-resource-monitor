"""Candidates for group reminders: fresh, attributable misuse of reserved cards."""
import hashlib
import json
import math
import re

EMPLOYEE=re.compile(r'[a-z][0-9]{8}\Z')
CLASSIFICATIONS={'已登记使用','未登记使用（疑似插队）','超时释放后仍有进程','到时释放后仍有进程'}


def violations(monitor,tasks,continuing,releases,now):
    if monitor is None:return []
    fresh=monitor.fresh_nodes(now);grouped={}
    for process in monitor.current(now):
        node=process['node'];employee=process['employee'];name=process['name']
        sampled=monitor.inventory_times.get(node,0)
        if (node not in fresh or not isinstance(sampled,(int,float)) or not math.isfinite(sampled)
                or not -15<=now-sampled<=30 or not EMPLOYEE.fullmatch(employee)
                or not name or len(name)>100 or any(ord(c)<32 for c in name)):
            continue
        if '@' in name or name.casefold() in {'所有人','全体成员','all','everyone'}:continue
        classification=monitor.classification(process,tasks,continuing,releases)
        if classification not in CLASSIFICATIONS:continue
        cards=monitor.cards(process)
        if cards is None:continue
        permitted=set(monitor.shared_cards(process,tasks,continuing))
        for task in tasks:
            if task['state']=='active' and task['employee']==employee:
                permitted.update(task['allocations'].get(node,[]))
        cards=cards-permitted
        matched=set();jobs=set()
        for task in tasks:
            if (task['state']!='active' or
                    employee in [task['employee'],*task.get('allowed_employees',[])]) :continue
            overlap=cards & set(task['allocations'].get(node,[]))
            if overlap:matched.update(overlap);jobs.add(task['id'])
        if not matched:continue
        item=grouped.setdefault((node,employee,name),dict(cards=set(),jobs=set(),sampled_at=sampled))
        item['cards'].update(matched);item['jobs'].update(jobs)
    result=[]
    for (node,employee,name),item in sorted(grouped.items()):
        cards=sorted(item['cards'])
        digest=hashlib.sha256(json.dumps([node,employee,cards,sorted(item['jobs'])],sort_keys=True).encode()).hexdigest()[:24]
        result.append(dict(id=digest,node=node,employee=employee,name=name,cards=cards,sampled_at=item['sampled_at']))
    return result

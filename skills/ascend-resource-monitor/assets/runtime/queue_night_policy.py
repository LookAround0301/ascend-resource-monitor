"""Pure allocation and enforcement decisions for fixed night reservations.

This module never stops a container. The caller must revalidate inventory,
reservation state and the container instance before executing a decision.
"""
from itertools import combinations
import math
import re


def reserve_night(nodes, reservations, start, seconds, cards, dual=False, fixed_node='', node_count=None, selected_nodes=None):
    """Choose cards for an exact half-open interval, or return None on conflict."""
    if (not 2<=len(nodes)<=32 or any(len(v) not in (8,16) for v in nodes.values())
            or len({len(v) for v in nodes.values()})!=1
            or not math.isfinite(start) or seconds<=0 or cards<=0):
        raise ValueError('invalid night reservation')
    capacity=len(next(iter(nodes.values())))
    node_count = (2 if dual else 1) if node_count is None else node_count
    if type(node_count) is not int or not 1<=node_count<=len(nodes) or (fixed_node and node_count>1):
        raise ValueError('invalid requested node count')
    if cards>capacity or (fixed_node and fixed_node not in nodes):
        raise ValueError('invalid night allocation')
    if selected_nodes is not None and (not isinstance(selected_nodes, list)
            or any(not isinstance(node, str) for node in selected_nodes)
            or len(set(selected_nodes)) != node_count or len(selected_nodes) != node_count
            or not set(selected_nodes) <= set(nodes)
            or (fixed_node and selected_nodes != [fixed_node])):
        raise ValueError('invalid selected night nodes')
    end=start+seconds
    used={node:set() for node in nodes}
    for reservation in reservations:
        if reservation['start']<end and start<reservation['end']:
            for node,occupied in reservation['allocations'].items():
                if node not in nodes or not set(occupied)<=set(nodes[node]):
                    raise ValueError('invalid existing night allocation')
                used[node].update(occupied)
    def choices(node):
        order=nodes[node];available=[card for card in order if card not in used[node]]
        half=capacity//2;groups=(order[:half],order[half:],order) if cards<=half else (order,)
        options=[]
        for group in groups:
            group_free=[card for card in group if card in available]
            if len(group_free)<cards:continue
            subsets=combinations(group_free,cards) if capacity==8 else [tuple(group_free[:cards])]
            for subset in subsets:
                remaining=set(available)-set(subset)
                intact=sum(set(g)<=remaining for g in (order[:half],order[half:]))
                options.append(((-intact,tuple(map(str,subset))),list(subset)))
        return sorted(options,key=lambda item:item[0])
    if node_count>1:
        selected={node:choices(node) for node in nodes}
        eligible=sorted((node for node in nodes if selected[node] and (selected_nodes is None or node in selected_nodes)),
                        key=lambda node:(capacity-len(used[node]),selected[node][0][0],str(node)))
        return {node:selected[node][0][1] for node in eligible[:node_count]} if len(eligible)>=node_count else None
    options=[]
    for node in nodes:
        if fixed_node and node!=fixed_node:continue
        if selected_nodes is not None and node not in selected_nodes:continue
        for score,selection in choices(node):
            options.append(((capacity-len(used[node]),score[0],str(node),score[1]),{node:selection}))
    return min(options,key=lambda item:item[0])[1] if options else None


def enforcement_decisions(tasks, rows, fresh_nodes, now):
    """Find unauthorized containers when a night allocation is starting.

    Unknown ownership, unknown card mapping or conflicting shared containers
    are reported as unresolved instead of asserting an unsafe target.
    """
    scope={};allowed={};active_allowed={}
    for task in tasks:
        start=task['not_before'];end=(task.get('activated') or start)+task['requested_seconds']
        if task.get('night_end_requested'):end=min(end,task['night_end_requested'])
        if start and start<=now<end and task['state']=='active':
            for node,cards in task['allocations'].items():
                for card in cards:
                    active_allowed.setdefault((node,card),set()).update([task['employee'],*task.get('allowed_employees',[])])
        if not start or now<start or now>=end or task['state']!='queued':continue
        for node,cards in task['allocations'].items():
            scope.setdefault(node,set()).update(cards)
            if now<end:
                for card in cards:
                    allowed.setdefault((node,card),set()).update([task['employee'],*task.get('allowed_employees',[])])
    grouped={};unresolved=[];protected_containers=set()
    for row in rows:
        node=row.get('node');cards=row.get('cards');employee=row.get('employee','');ref=row.get('control_ref','')
        if node in scope and cards is not None and employee and ref:
            if any(employee in active_allowed.get((node,card),set()) for card in cards):
                protected_containers.add((node,ref))
    for node in scope:
        if node not in fresh_nodes:unresolved.append({'node':node,'reason':'inventory_unavailable'})
    for row in rows:
        node=row['node']
        if node not in scope or node not in fresh_nodes:continue
        cards=row.get('cards')
        if cards is None:
            unresolved.append({'node':node,'reason':'unknown_cards'});continue
        overlap=set(cards)&scope[node]
        if not overlap:continue
        employee=row.get('employee','')
        ref=row.get('control_ref','')
        if not employee or employee in ('未知','待确认') or not re.fullmatch(r'[0-9a-f]{64}@.+',ref):
            unresolved.append({'node':node,'reason':'unknown_owner_or_container'});continue
        group=grouped.setdefault((node,ref),{'authorized':(node,ref) in protected_containers,'unauthorized':False,'employees':set(),'cards':set(),'unauthorized_cards':set(),'process_keys':set()})
        group['authorized'] |= any(employee in allowed.get((node,card),set()) for card in overlap)
        group['unauthorized'] |= any(employee not in allowed.get((node,card),set()) for card in overlap)
        group['unauthorized_cards'].update(card for card in overlap if employee not in allowed.get((node,card),set()))
        group['employees'].add(employee);group['cards'].update(overlap)
        group['process_keys'].add(row['process_key'])
    targets=[]
    for (node,ref),item in grouped.items():
        if item['authorized'] and item['unauthorized']:
            unresolved.append({'node':node,'reason':'shared_container_conflict','control_ref':ref,
                               'cards':sorted(item['unauthorized_cards'])})
        elif item['unauthorized']:
            targets.append({'node':node,'control_ref':ref,'employees':sorted(item['employees']),
                            'cards':sorted(item['cards']),'process_keys':sorted(item['process_keys']),
                            'allowed':{str(card):sorted(allowed.get((node,card),set())) for card in sorted(scope[node])}})
    return targets,unresolved

"""Move unstarted night reservations behind higher-priority card intervals."""
import math


def reschedule(nights, blockers):
    """Return stable, non-overlapping starts without moving a reservation earlier.

    Each night has id, requested, scheduled, seconds and allocations.  Blockers
    have start, end (None means unknown) and allocations.  Night card
    allocations are preserved; requests are processed by requested start.
    """
    occupied=list(blockers)
    result={}
    for night in sorted(nights,key=lambda item:(item['requested'],item['order'],item['id'])):
        start=max(night['requested'],night['scheduled'])
        while True:
            conflicts=[]
            for item in occupied:
                end=math.inf if item['end'] is None else item['end']
                if item['start']>=start+night['seconds'] or end<=start:
                    continue
                if any(set(cards).intersection(item['allocations'].get(node,()))
                       for node,cards in night['allocations'].items()):
                    conflicts.append(end)
            if not conflicts:
                result[night['id']]=start
                occupied.append({'start':start,'end':start+night['seconds'],
                                 'allocations':night['allocations']})
                break
            new_start=max(conflicts)
            if not math.isfinite(new_start):
                result[night['id']]=None
                # Reserve these cards indefinitely for downstream night jobs.
                occupied.append({'start':start,'end':None,'allocations':night['allocations']})
                break
            start=new_start
    return result

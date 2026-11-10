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
            end=min(start+night['seconds'],night.get('latest_end',math.inf))
            if end-start<night.get('minimum_seconds',0) or end<=start:
                result[night['id']]=None
                break
            conflicts=[]
            for item in occupied:
                occupied_end=math.inf if item['end'] is None else item['end']
                if item['start']>=end or occupied_end<=start:
                    continue
                if any(set(cards).intersection(item['allocations'].get(node,()))
                       for node,cards in night['allocations'].items()):
                    conflicts.append(occupied_end)
            if not conflicts:
                result[night['id']]=start
                occupied.append({'start':start,'end':end,
                                 'allocations':night['allocations']})
                break
            new_start=max(conflicts)
            if not math.isfinite(new_start):
                result[night['id']]=None
                # Reserve these cards indefinitely for downstream night jobs.
                latest=night.get('latest_end')
                occupied.append({'start':start,'end':latest if latest and math.isfinite(latest) else None,'allocations':night['allocations']})
                break
            start=new_start
    return result

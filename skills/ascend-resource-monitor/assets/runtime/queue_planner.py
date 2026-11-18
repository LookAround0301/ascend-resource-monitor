"""Pure reservation planning for a resource pool with 8 or 16 cards per node.

This module does not read hardware, start or stop jobs, or persist allocations.
Callers must serialize planning and persistence and supply trustworthy intervals.
"""
from itertools import combinations
import math


def plan(nodes, busy, jobs, now, buffer_seconds=600):
    """Plan jobs in submitted order, allowing non-delaying backfill.

    ``nodes`` maps node names to 8 or 16 ordered card identifiers; order defines
    two equal card groups. Busy entries have node, cards, start and end;
    ``None`` end means the release time is unknown. Entries represent still
    occupied or reserved resources; remove them only after explicit release.
    An overdue entry has unknown release time, regardless of its old estimate.
    Jobs have id, cards,
    seconds, optional ``not_before``, optional exclusive ``start_before``,
    and optional ``dual`` (equal card counts
    on both nodes, with ``cards`` specifying the count per node).
    Returned times are estimates based on declared durations, not guarantees.
    """
    capacity = len(next(iter(nodes.values()), ()))
    if not 2 <= len(nodes) <= 32 or capacity not in (8, 16) or any(len(cards) != capacity or len(set(cards)) != capacity for cards in nodes.values()):
        raise ValueError('2 to 32 nodes with the same 8 or 16 distinct ordered cards each are required')
    half = capacity // 2
    if buffer_seconds < 0 or not math.isfinite(now):
        raise ValueError('invalid planning time or buffer')
    intervals = []
    fixed_nights = []
    for item in busy:
        node = item['node']
        cards = tuple(item['cards'])
        end = math.inf if item['end'] is None or item['end'] <= now else item['end']
        direct_end = item.get('direct_end', item['end'])
        direct_end = math.inf if direct_end is None or direct_end <= now else direct_end
        if node not in nodes or not cards or len(set(cards)) != len(cards) or not set(cards) <= set(nodes[node]):
            raise ValueError('invalid busy cards')
        if end <= item['start']:
            raise ValueError('invalid busy interval')
        intervals.append((node, frozenset(cards), item['start'], end, direct_end, False, item.get('avoid_backfill_until')))
        if item.get('fixed_night'):
            fixed_nights.append((node, frozenset(cards), item['start']))

    def free(node, start, end, night, ignore_backfill_boundary=None):
        used = set()
        for occupied_node, cards, begin, buffered, direct, _, backfill_boundary in intervals:
            if ignore_backfill_boundary is not None and backfill_boundary == ignore_backfill_boundary:
                continue
            finish = direct if night else buffered
            if occupied_node == node and begin < end and start < finish:
                used.update(cards)
        return [card for card in nodes[node] if card not in used]

    def card_choices(node, count, available):
        choices = []
        ordered = nodes[node]
        groups = (ordered[:half], ordered[half:]) if count <= half else (ordered,)
        for group in groups:
            if capacity == 8:
                # Preserve A5 ordering and all original candidate choices exactly.
                for choice in combinations([card for card in group if card in available], count):
                    remaining = set(available) - set(choice)
                    intact_groups = sum(set(g) <= remaining for g in (ordered[:4], ordered[4:]))
                    choices.append(((-intact_groups, tuple(map(str, choice))), list(choice)))
                continue
            choice = [card for card in group if card in available][:count]
            if len(choice) != count:
                continue
            # Within a group, every subset has the same intact-group score.
            # Use configured card order without enumerating 16-card combinations.
            remaining = set(available) - set(choice)
            intact_groups = sum(set(g) <= remaining for g in (ordered[:half], ordered[half:]))
            choices.append(((-intact_groups, tuple(map(str, choice))), choice))
        return sorted(choices, key=lambda pair: pair[0])

    def options(job, start, seconds, no_buffer=False, ignore_backfill_boundary=None):
        boundary=job.get('clip_finish_before',math.inf)
        # Durations are whole seconds but the clock may have a fractional part.
        # A clipped job can therefore end less than one second before the
        # boundary; do not add a buffer that would overlap the night booking.
        reaches_midnight=(job.get('midnight_separation') and math.isfinite(boundary)
                          and seconds>=math.floor(boundary-start))
        end = start + seconds + (0 if reaches_midnight or no_buffer else buffer_seconds)
        available = {node: free(node, start, end, job.get('night', False), ignore_backfill_boundary) for node in nodes}
        count = job['cards']
        node_count = job.get('node_count', 2 if job.get('dual') else 1)
        if node_count > 1:
            choices = {node: card_choices(node, count, cards) for node, cards in available.items()}
            eligible = sorted((node for node in nodes if choices[node] and node in job.get('allowed_nodes', nodes)),
                              key=lambda node: (len(available[node]), choices[node][0][0], str(node)))
            if len(eligible) < node_count:
                return []
            return [{node: choices[node][0][1] for node in eligible[:node_count]}]
        choices = []
        allowed = job.get('allowed_nodes', nodes)
        for node in nodes:
            if node not in allowed:
                continue
            for score, choice in card_choices(node, count, available[node]):
                # Fill the more occupied node first, preserving whole card groups.
                choices.append(((len(available[node]), score[0], str(node), score[1]), {node: choice}))
        return [choice for _, choice in sorted(choices, key=lambda pair: pair[0])]

    planned = []
    prior_reservations = []
    for job in jobs:
        node_count = job.get('node_count', 2 if job.get('dual') else 1)
        if type(node_count) is not int or not 1 <= node_count <= len(nodes):
            raise ValueError('invalid requested node count')
        if (type(job.get('cards')) is not int or not 1 <= job['cards'] <= capacity
                or (node_count > 1 and job['cards'] not in tuple(n for n in (1, 2, 4, 8, 16) if n <= capacity))
                or type(job.get('seconds')) is not int or job['seconds'] <= 0):
            raise ValueError('invalid job size or duration')
        if 'allowed_nodes' in job and (not isinstance(job['allowed_nodes'], list)
                or len(set(job['allowed_nodes'])) != len(job['allowed_nodes'])
                or not set(job['allowed_nodes']) <= set(nodes)):
            raise ValueError('invalid allowed nodes')
        earliest = max(now, job.get('not_before', now))
        candidates = {earliest}
        candidates.update((direct if job.get('night') else end) for _, _, _, end, direct, _, _ in intervals
                          if math.isfinite(direct if job.get('night') else end) and (direct if job.get('night') else end) >= earliest)
        avoid=job.get('avoid_windows',())
        def next_permitted(value):
            for begin,end in sorted(avoid):
                if begin<=value<end:value=end
            return value
        raw_candidates=candidates
        candidates={next_permitted(value) for value in raw_candidates}
        chosen = None
        for start in sorted(candidates):
            if job.get('start_before') and start >= job['start_before']:
                continue
            seconds=job['seconds']
            if job.get('clip_finish_before'):
                seconds=min(seconds,math.floor(job['clip_finish_before']-start))
                if seconds<60:
                    continue
            durations={seconds}
            truncation_sources={}
            if job.get('clip_finish_before'):
                for node,cards,begin in fixed_nights:
                    duration=math.floor(begin-start-(0 if job.get('midnight_separation') and begin==job.get('clip_finish_before') else buffer_seconds))
                    if begin>start and 60<=duration<seconds:
                        durations.add(duration)
                        truncation_sources.setdefault(duration,[]).append((node,cards))
            minimum=job.get('backfill_min_seconds',0)
            if minimum and not job.get('night'):
                for prior in prior_reservations:
                    duration=math.floor(prior['start']-start-(0 if prior['avoid_forced'] else buffer_seconds))
                    if minimum<=duration<seconds:
                        durations.add(duration)
                        truncation_sources.setdefault(duration,[]).extend((node,frozenset(cards)) for node,cards in prior['allocations'].items())
            for candidate_seconds in sorted(durations,reverse=True):
                if job.get('finish_before') and start+candidate_seconds>job['finish_before']:
                    continue
                available=options(job,start,candidate_seconds)
                if candidate_seconds<seconds:
                    available=[allocation for allocation in available
                               if any(cards.intersection(allocation.get(node,()))
                                      for node,cards in truncation_sources[candidate_seconds])]
                no_buffer=False
                if not available and minimum and not job.get('night'):
                    available=options(job,start,candidate_seconds,no_buffer=True)
                    if candidate_seconds<seconds:
                        available=[allocation for allocation in available
                                   if any(cards.intersection(allocation.get(node,()))
                                          for node,cards in truncation_sources[candidate_seconds])]
                    end=start+candidate_seconds
                    def permissible_tail(allocation):
                        conflicts=[avoid_forced for node,cards,begin,_,_,avoid_forced,_ in intervals
                                   if end<=begin<end+buffer_seconds
                                   and cards.intersection(allocation.get(node,()))]
                        return bool(conflicts) and all(conflicts)
                    available=[allocation for allocation in available if permissible_tail(allocation)]
                    no_buffer=bool(available)
                if available:
                    chosen=(start,available[0],candidate_seconds,no_buffer)
                    break
            if chosen:break
        if chosen is None:
            planned.append({'id': job['id'], 'start': None, 'allocations': {}})
            if job.get('finish_before') or job.get('start_before'):
                # A truly expired request cannot reserve resources. If a still
                # eligible request is blocked by an unknown release, later jobs
                # must not take resources it might need when that release occurs.
                latest = min(job.get('start_before') or math.inf,
                             (job['finish_before'] - job['seconds']) if job.get('finish_before') else math.inf)
                allowed = set(job.get('allowed_nodes', nodes))
                unknown = any(node in allowed and not math.isfinite(direct if job.get('night') else end)
                              for node, _, _, end, direct, _, _ in intervals)
                if latest > earliest and unknown:
                    protect_until = latest + job['seconds'] + buffer_seconds
                    for node in nodes:
                        if node in allowed:
                            intervals.append((node, frozenset(nodes[node]), earliest, protect_until, protect_until, False, None))
                continue
            # Without a bound for this job, accepting later work can starve it.
            planned.extend({'id': later['id'], 'start': None, 'allocations': {}} for later in jobs[len(planned):])
            break
        start, allocations,seconds,no_buffer = chosen
        # A coincidental avoid-window end is insufficient: the selected cards
        # must have been available for the same job at an earlier candidate.
        avoid_forced=any(t<start and next_permitted(t)==start
                         and allocations in options(job,t,seconds,ignore_backfill_boundary=start)
                         for t in raw_candidates)
        finish = start + seconds + (0 if no_buffer else buffer_seconds)
        relevant=[p['start'] for p in prior_reservations if p['start']>start and
                  any(set(cards).intersection(p['allocations'].get(node,())) for node,cards in allocations.items())]
        backfill_boundary=min(relevant) if no_buffer and relevant else None
        for node, cards in allocations.items():
            direct_end = start + seconds
            intervals.append((node, frozenset(cards), start, finish, direct_end, avoid_forced, backfill_boundary))
        result={'id': job['id'], 'start': start, 'allocations': allocations, 'seconds':seconds}
        if no_buffer:
            if relevant:
                result['backfill_end_before']=min(relevant)
                result['backfill_no_buffer']=True
        elif job.get('backfill_min_seconds') and seconds<job['seconds']:
            if relevant and start+seconds+buffer_seconds<=min(relevant):
                result['backfill_end_before']=min(relevant)-buffer_seconds
        planned.append(result)
        prior_reservations.append({'start':start,'allocations':allocations,'avoid_forced':avoid_forced})
    return planned

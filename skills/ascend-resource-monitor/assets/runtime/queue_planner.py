"""Pure reservation planning for a two-node, eight-card resource pool.

This module does not read hardware, start or stop jobs, or persist allocations.
Callers must serialize planning and persistence and supply trustworthy intervals.
"""
from itertools import combinations
import math


def plan(nodes, busy, jobs, now, buffer_seconds=600):
    """Plan jobs in submitted order, allowing non-delaying backfill.

    ``nodes`` maps node names to eight ordered card identifiers; order defines
    the two groups of four. Busy entries have node, cards, start and end;
    ``None`` end means the release time is unknown. Entries represent still
    occupied or reserved resources; remove them only after explicit release.
    An overdue entry has unknown release time, regardless of its old estimate.
    Jobs have id, cards,
    seconds, optional ``not_before``, optional exclusive ``start_before``,
    and optional ``dual`` (equal card counts
    on both nodes, with ``cards`` specifying the count per node).
    Returned times are estimates based on declared durations, not guarantees.
    """
    if len(nodes) != 2 or any(len(cards) != 8 or len(set(cards)) != 8 for cards in nodes.values()):
        raise ValueError('two nodes with eight distinct ordered cards each are required')
    if buffer_seconds < 0 or not math.isfinite(now):
        raise ValueError('invalid planning time or buffer')
    intervals = []
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
        intervals.append((node, frozenset(cards), item['start'], end, direct_end))

    def free(node, start, end, night):
        used = set()
        for occupied_node, cards, begin, buffered, direct in intervals:
            finish = direct if night else buffered
            if occupied_node == node and begin < end and start < finish:
                used.update(cards)
        return [card for card in nodes[node] if card not in used]

    def card_choices(node, count, available):
        choices = []
        ordered = nodes[node]
        groups = (ordered[:4], ordered[4:]) if count <= 4 else (ordered,)
        for group in groups:
            for choice in combinations([card for card in group if card in available], count):
                remaining = set(available) - set(choice)
                intact_groups = sum(set(g) <= remaining for g in (ordered[:4], ordered[4:]))
                choices.append(((-intact_groups, tuple(map(str, choice))), list(choice)))
        return sorted(choices, key=lambda pair: pair[0])

    def options(job, start):
        end = start + job['seconds'] + buffer_seconds
        available = {node: free(node, start, end, job.get('night', False)) for node in nodes}
        count = job['cards']
        if job.get('dual'):
            choices = {node: card_choices(node, count, cards) for node, cards in available.items()}
            return [{node: cards[0][1] for node, cards in choices.items()}] if all(choices.values()) else []
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
    for job in jobs:
        if (type(job.get('cards')) is not int or not 1 <= job['cards'] <= 8
                or (job.get('dual') and job['cards'] not in (1, 2, 4, 8))
                or type(job.get('seconds')) is not int or job['seconds'] <= 0):
            raise ValueError('invalid job size or duration')
        earliest = max(now, job.get('not_before', now))
        candidates = {earliest}
        candidates.update((direct if job.get('night') else end) for _, _, _, end, direct in intervals
                          if math.isfinite(direct if job.get('night') else end) and (direct if job.get('night') else end) >= earliest)
        chosen = None
        for start in sorted(candidates):
            if job.get('start_before') and start >= job['start_before']:
                continue
            if job.get('finish_before') and start + job['seconds'] > job['finish_before']:
                continue
            available = options(job, start)
            if available:
                chosen = (start, available[0])
                break
        if chosen is None:
            planned.append({'id': job['id'], 'start': None, 'allocations': {}})
            if job.get('finish_before') or job.get('start_before'):
                # A truly expired request cannot reserve resources. If a still
                # eligible request is blocked by an unknown release, later jobs
                # must not take resources it might need when that release occurs.
                latest = min(job.get('start_before') or math.inf,
                             (job['finish_before'] - job['seconds']) if job.get('finish_before') else math.inf)
                allowed = set(nodes) if job.get('dual') else set(job.get('allowed_nodes', nodes))
                unknown = any(node in allowed and not math.isfinite(direct if job.get('night') else end)
                              for node, _, _, end, direct in intervals)
                if latest > earliest and unknown:
                    protect_until = latest + job['seconds'] + buffer_seconds
                    for node in nodes:
                        if node in allowed:
                            intervals.append((node, frozenset(nodes[node]), earliest, protect_until, protect_until))
                continue
            # Without a bound for this job, accepting later work can starve it.
            planned.extend({'id': later['id'], 'start': None, 'allocations': {}} for later in jobs[len(planned):])
            break
        start, allocations = chosen
        finish = start + job['seconds'] + buffer_seconds
        for node, cards in allocations.items():
            direct_end = start + job['seconds']
            intervals.append((node, frozenset(cards), start, finish, direct_end))
        planned.append({'id': job['id'], 'start': start, 'allocations': allocations})
    return planned

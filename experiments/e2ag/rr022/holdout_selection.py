"""Predeclared metadata-only selection, no method or report-quality outcomes."""
from collections import Counter
import hashlib


def select_objects(rows, protocol, exclusions):
    ids, groups, heads = (set(exclusions[k]) for k in ('event_ids', 'object_groups', 'head_shas'))
    strata = {(s['repository'], s['workflow']): s['maximum_objects'] for s in protocol['strata']}
    counts, decisions, events, representatives = Counter(), [], {}, {}
    for row in sorted(rows, key=lambda r: (r['created_at'], int(r['event_id']), r['archive_hour'], r['line'])):
        key = (row['repository'].lower(), row.get('workflow'))
        reason = None
        if row['event_id'] in events:
            reason = 'duplicate_event'
        elif row['archive_hour'][:10] in exclusions['excluded_dates']:
            reason = 'exploration_date'
        elif not protocol['window']['start_utc'] <= row['created_at'] < protocol['window']['end_utc_exclusive']:
            reason = 'outside_created_at_window'
        elif key not in strata:
            reason = 'outside_strata'
        elif not row['inspection_status'].startswith('metadata_eligible_'):
            reason = 'ineligible_metadata'
        elif row['event_id'] in ids or row['object_group'].lower() in groups or row.get('head_sha') in heads:
            reason = 'previous_exposure'
        events[row['event_id']] = row
        if reason is None:
            group = row['object_group'].lower()
            if group in representatives:
                reason = 'later_object_event'
            else:
                representatives[group] = row
        if reason:
            counts[reason] += 1
            decisions.append(dict(event_id=row['event_id'], object_group=row.get('object_group'), reason=reason))
    unique, used_heads = [], {}
    for group, row in sorted(representatives.items()):
        head = row.get('head_sha')
        if head and head in used_heads:
            counts['shared_head'] += 1
            decisions.append(dict(event_id=row['event_id'], object_group=group, reason='shared_head', retained_group=used_heads[head]))
        else:
            unique.append(row)
            if head: used_heads[head] = group
    selected, summaries = [], []
    for key, limit in strata.items():
        eligible = [r for r in unique if (r['repository'].lower(), r['workflow']) == key]
        def rank(row):
            text = '\n'.join((protocol['selection']['seed'], key[0], key[1], row['object_group'].lower()))
            return hashlib.sha256(text.encode()).hexdigest(), row['object_group'].lower()
        ordered = sorted(eligible, key=rank)
        for index, row in enumerate(ordered[:limit], 1):
            selected.append({**row, 'rank': index, 'rank_sha256': rank(row)[0]})
        summaries.append(dict(repository=key[0], workflow=key[1], eligible=len(eligible), selected=min(limit, len(eligible)), maximum=limit))
        for row in ordered[limit:]:
            decisions.append(dict(event_id=row['event_id'], object_group=row['object_group'], reason='rank_beyond_limit'))
    return dict(selected=selected, strata=summaries, exclusions=decisions, exclusion_counts=dict(counts), independent_objects=len(selected))

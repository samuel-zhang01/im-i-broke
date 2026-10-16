"""Read-only daily investment history projected onto verified account identities.

Current broker totals remain authoritative. Historical reconstruction is a
separate, explicitly estimated series; conflicting provider estimates cannot
be interleaved with statement-backed values to manufacture price movements.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from . import market_cache


def recorded_cutover(account):
    """First collection day is durable even when the broker's balance is older."""
    if account.get('source') != 'snaptrade':
        return None
    days = []
    try:
        days.append(date.fromisoformat(account.get('recorded_only_from')).isoformat())
    except (TypeError, ValueError):
        pass
    for point in account['observations']:
        if point.get('source') != 'recorded':
            continue
        for raw in (point.get('retrieved_at'), point.get('date')):
            try:
                parsed = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
                when = parsed.astimezone(timezone.utc).date() if parsed.tzinfo else parsed.date()
                days.append(when.isoformat())
                break
            except (TypeError, ValueError):
                continue
    return min(days, default=None)


def observation_coverage(account, *, first_date=None):
    """Describe dated evidence, excluding carried balances and missing totals."""
    recorded = sorted({point['date'] for point in account['observations']
                       if point.get('source') == 'recorded' and point.get('value') is not None
                       and (first_date is None or point['date'] >= first_date)})
    reconstructed = [point['date'] for point in account.get('history_observations', [])
                     if point.get('source') == 'reconstructed' and point.get('value') is not None]
    return {'recorded_start_date': recorded[0] if recorded else None,
            'recorded_end_date': recorded[-1] if recorded else None,
            'recorded_days': len(recorded),
            'recorded_only_from': account.get('recorded_only_from'),
            'reconstructed_through': max(reconstructed, default=None),
            'current_value_as_of': account.get('as_of'),
            'source_time_known': account.get('source_time_known', False)}


def history_continuity(points, accounts):
    """Expose why complete portfolio history stops without filling its gaps."""
    identifiers = {account['id'] for account in accounts}
    recorded_sets = [{point['date'] for point in account.get('history_observations', [])
                      if point.get('source') == 'recorded' and point.get('value') is not None}
                     for account in accounts]
    shared_recorded = set.intersection(*recorded_sets) if recorded_sets else set()
    recorded_dates = [point['date'] for point in points if point['complete'] and point['date'] in shared_recorded]
    gaps, segments, segment, previous_key, gap = [], [], [], None, None
    for point in points:
        if not point['complete']:
            if segment:
                segments.append(segment)
                segment = []
            previous_key = None
            if gap is None:
                gap = {'start_date': point['date'], 'account_ids': set()}
            gap['account_ids'].update(identifiers - set(point['_known']))
            continue
        if gap is not None:
            end = date.fromisoformat(point['date']) - timedelta(days=1)
            gaps.append({'start_date': gap['start_date'], 'end_date': end.isoformat(),
                         'days': (end - date.fromisoformat(gap['start_date'])).days + 1,
                         'reason': 'missing_dated_account_values',
                         'account_ids': sorted(gap['account_ids'])})
            gap = None
        key = (point['coverage_key'], point['valuation_key'])
        if segment and (key != previous_key or point.get('source_break')):
            segments.append(segment)
            segment = []
        segment.append(point['date'])
        previous_key = key
    if segment:
        segments.append(segment)
    # Callers trim leading/trailing incomplete observations first. A single
    # new observation is visible, but does not establish a continuous line.
    continuous = [segment for segment in segments if len(segment) >= 2]
    return {'last_continuous_date': continuous[-1][-1] if continuous else None,
            'recorded_start_date': recorded_dates[0] if recorded_dates else None,
            'recorded_end_date': recorded_dates[-1] if recorded_dates else None,
            'recorded_days': len(recorded_dates), 'gaps': gaps}


def add_history(conn, accounts, cached, rates, *, today, cutoff=None, selected_ids=None):
    from .investment_history import reconstruct

    report = reconstruct(conn, cached=cached or {}, markets=market_cache.read_market_data(conn),
                         rates=rates, today=today)
    lookup = {account['id']: account for account in accounts}
    current_connected_ids = {row['id'] for row in (cached or {}).get('accounts', [])
                             if row.get('id') and not row.get('is_paper')}
    for account in accounts:
        account.pop('history_alternatives', None)
    reconstructed = defaultdict(list)
    issues = []
    for source_id, source in report.get('sources', {}).items():
        account = lookup.get(source_id)
        if account is None:
            continue
        target = lookup.get(account.get('canonical_id', source_id), account)
        reconstructed[target['id']].extend(source.get('observations', []))
        coverage = source.get('coverage', {})
        if target['id'] != source_id:
            coverage = {**coverage, 'identity_link_required': False, 'canonical_account_id': target['id']}
        target.setdefault('history_reconstruction', []).append(coverage)
        matched_id = coverage.get('quantity_matched_account_id')
        candidate = lookup.get('connected:' + matched_id) if isinstance(matched_id, str) else None
        if (target['id'] == source_id and source_id.startswith('statement:')
                and coverage.get('identity_link_required') is True
                and coverage.get('quantities_reconciled') is True
                and coverage.get('valued_days', 0) > 0
                and matched_id in current_connected_ids and candidate is not None
                and candidate.get('kind') == 'investment'
                and (selected_ids is None or candidate['id'] in selected_ids)):
            # Offer the separate source for inspection only. Matching closing
            # quantities do not authorize merging history or changing identity.
            candidate.setdefault('history_alternatives', []).append({
                'source_id': source_id, 'name': account['name'],
                'start_date': coverage.get('start_date'), 'end_date': coverage.get('end_date'),
                'valued_days': coverage['valued_days'],
                'reconciled_holdings': coverage.get('reconciled_holdings', 0),
                'quantity_check_date': coverage.get('quantity_check_date'),
                'identity_link_required': True,
            })
        if selected_ids is None or target['id'] in selected_ids:
            for message in source.get('issues', []):
                issues.append(f"{target['name']}: {message}")

    for account in accounts:
        # Resolve this before merging imports. Its source is the saved receipt
        # archive, never a chart range, inferred trade or reconstructed NAV.
        cutovers = [recorded_cutover(lookup[identifier]) for identifier in account.get('source_ids', [account['id']])]
        recorded_from = min((when for when in cutovers if when), default=None)
        if recorded_from:
            account['recorded_only_from'] = recorded_from
            account['forced_stale'] = account.get('forced_stale', False) or any(
                lookup[identifier].get('forced_stale', False)
                for identifier in account.get('source_ids', [account['id']])
                if lookup[identifier].get('source') == 'snaptrade')
        else:
            recorded_from = None
        account['history_observations'] = [point for point in account['observations']
            if not recorded_from or point['date'] < recorded_from or point.get('source') == 'recorded']
        if account['kind'] != 'investment' or not reconstructed[account['id']]:
            continue
        daily = {}
        for point in reconstructed[account['id']]:
            if recorded_from and point['date'] >= recorded_from:
                continue  # Imported history cannot replace or fill live collection days.
            old = daily.get(point['date'])
            if old and old['value'] != point['value']:
                daily[point['date']] = {**point, 'value': None, 'reconciled': False}
            else:
                daily[point['date']] = point
        # A material disagreement on a supported same-day comparison means
        # provider history is not a safe continuation of the statement replay.
        conflicts = []
        for point in account['observations']:
            reference = daily.get(point['date'])
            if point.get('source') != 'snaptrade_estimate' or not reference or reference['value'] is None:
                continue
            reported = rates.convert(point['value'], point['currency'], 'GBP', point['date'])
            if reported is not None and abs(reported - reference['value']) > max(10, abs(reference['value']) * .05):
                conflicts.append(point['date'])
        if conflicts:
            account['history_provider_conflict'] = {'dates': len(conflicts), 'first_date': min(conflicts), 'threshold_pct': 5}
            if selected_ids is None or account['id'] in selected_ids:
                issues.append(f"{account['name']}: provider history disagrees with the reconstructed holdings and cash by more than 5% on {len(conflicts)} comparable dates. Those provider estimates are excluded from this history.")
        points = {point['date']: point for point in account['history_observations']
                  if not (conflicts and point.get('source') == 'snaptrade_estimate')}
        # Before live collection, use one consistent historical market/FX basis.
        # All original imported and provider records remain in their sources.
        for when, point in daily.items():
            if (point.get('overlaps_daily_reports') and point.get('source_statement_type') == 'monthly'
                    and points.get(when, {}).get('source') == 'statement'
                    and points[when].get('value') is not None):
                # A daily report's independently printed opening/closing NAV
                # is stronger evidence on that date than a later monthly
                # replay using reference closes and FX.
                continue
            if point['value'] is not None or when not in points:
                points[when] = point
        account['history_observations'] = [points[when] for when in sorted(points)]
    # Provider history can contain a large level change even when no verified
    # statement link exists. Preserve its values, but do not draw or calculate
    # a continuous comparison across an unexplained discontinuity.
    for account in accounts:
        if account['kind'] != 'investment':
            continue
        segment, breaks, previous = 0, 0, None
        projected = []
        for original in account['history_observations']:
            source_break = False
            if previous is not None:
                kinds = {previous.get('source'), original.get('source')}
                gap = (date.fromisoformat(original['date']) - date.fromisoformat(previous['date'])).days
                if 'snaptrade_estimate' in kinds and 'reconstructed' not in kinds and 0 < gap <= 7:
                    before = rates.convert(previous['value'], previous['currency'], 'GBP', previous['date'])
                    after = rates.convert(original['value'], original['currency'], 'GBP', original['date'])
                    if before is not None and after is not None and abs(after - before) > max(1000, abs(before) * .5):
                        source_break = True
                        segment += 1
                        breaks += 1
            projected.append({**original, 'comparison_segment': segment, 'source_break': source_break})
            previous = original
        account['history_observations'] = projected
        account['history_discontinuities'] = breaks
        if breaks and (selected_ids is None or account['id'] in selected_ids):
            issues.append(f"{account['name']}: provider history contains {breaks} large {'change' if breaks == 1 else 'changes'} "
                          'over seven days or less. These may reflect cash flows or incomplete provider history, rather than investment returns. '
                          'Values are preserved and the comparison line breaks at those dates.')
    return list(dict.fromkeys(issues))

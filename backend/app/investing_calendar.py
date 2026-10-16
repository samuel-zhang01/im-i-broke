"""Cash-flow-inclusive account-value calendar from saved, dated observations.

Neither a partial activity feed nor current holdings establish past personal
profit. Estimated account history and recorded snapshots therefore stay in
separate series, and unobserved days are never filled with zero or carried NAV.
"""
from __future__ import annotations

from calendar import monthrange
from bisect import bisect_left
from datetime import date, datetime, timedelta, timezone

from . import snaptrade_store, wealth
from .snaptrade_models import currency, number


SOURCES = {'estimate': 'SnapTrade estimates', 'recorded': 'Recorded snapshots'}


def _date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _days(first, last):
    while first <= last:
        yield first
        first += timedelta(days=1)


def _observations(accounts, source, estimates, observations, today):
    """One latest observation per account/day, with ambiguous values withheld."""
    ids = {a['id'] for a in accounts}
    result = {}
    rows = list(estimates if source == 'estimate' else observations)
    if source == 'estimate':
        persisted = {(row.get('account_id'), str(row.get('date'))[:10]) for row in rows}
        for account in accounts:
            for point in account.get('history', {}).get('data', []):
                if (account['id'], str(point.get('date'))[:10]) not in persisted:
                    rows.append({**point, 'account_id': account['id'], 'retrieved_at': ''})
    for row in rows:
        account_id = row.get('account_id')
        when = _date(row.get('date') if source == 'estimate' else row.get('observed_at'))
        if account_id not in ids or when is None or when > today:
            continue
        value, code = number(row.get('total_value')), currency(row.get('currency'))
        # A later explicit unknown must not silently resurrect an older total.
        rank = (str(row.get('observed_at') or row.get('date') or ''), str(row.get('retrieved_at') or ''))
        key = (account_id, when.isoformat())
        old = result.get(key)
        if old is not None and old['rank'] > rank:
            continue
        if old is not None and old['rank'] == rank and (old['value'] != value or old['currency'] != code):
            result[key] = {'value': None, 'currency': None, 'rank': rank, 'conflict': True}
        else:
            result[key] = {'value': value, 'currency': code, 'rank': rank, 'conflict': False}
    return result


def bundle(conn, *, path=None, currency_code='GBP', year=None, month=None, source='estimate',
           account_ids=None, paper=False, today=None, cached=None, estimates=None, observations=None):
    today = today or datetime.now(timezone.utc).date()
    year = today.year if year is None else year
    month = today.month if month is None else month
    if not currency(currency_code) or source not in SOURCES:
        raise ValueError('Choose a supported currency and calendar source.')
    if isinstance(year, bool) or not isinstance(year, int) or not 1970 <= year <= today.year + 1:
        raise ValueError('Choose a valid calendar year.')
    if isinstance(month, bool) or not isinstance(month, int) or not 1 <= month <= 12:
        raise ValueError('Choose a valid calendar month.')
    cached = snaptrade_store.load_portfolio(path=path) if cached is None else cached
    cached = cached or {'accounts': [], 'connections': {'data': []}}
    accounts = cached.get('accounts', [])
    requested = snaptrade_store.selected_account_ids(accounts, account_ids, paper=paper)
    selected = [a for a in accounts if a['id'] in requested]
    modes = {bool(a.get('is_paper')) for a in selected}
    if len(modes) > 1:
        raise ValueError('View live and paper accounts separately.')
    paper = next(iter(modes)) if modes else paper
    estimates = snaptrade_store.list_account_estimates(path=path, account_ids=requested) if estimates is None else estimates
    observations = snaptrade_store.list_account_observations(path=path, account_ids=requested) if observations is None else observations
    data = {kind: _observations(selected, kind, estimates, observations, today) for kind in SOURCES}
    chosen = data[source]
    rates = wealth.Rates(conn)
    values = {}

    def total(when):
        day = when.isoformat()
        if day in values:
            return values[day]
        amounts = []
        if when <= today:
            for account_id in requested:
                observation = chosen.get((account_id, day))
                amount = rates.convert(observation['value'], observation['currency'], currency_code, day) if observation else None
                if amount is not None:
                    amounts.append(amount)
        complete = bool(requested) and len(amounts) == len(requested)
        values[day] = {'date': day, 'value': sum(amounts) if complete else None,
                       'known_accounts': len(amounts), 'complete': complete,
                       'estimated': source == 'estimate'}
        return values[day]

    comparable_dates = sorted({day for _, day in chosen if total(date.fromisoformat(day))['complete']})

    def cell(when):
        start, end = total(when - timedelta(days=1)), total(when)
        complete = start['complete'] and end['complete']
        change = end['value'] - start['value'] if complete else None
        previous_index = bisect_left(comparable_dates, when.isoformat()) - 1
        previous_date = comparable_dates[previous_index] if end['complete'] and previous_index >= 0 else None
        previous = total(date.fromisoformat(previous_date)) if previous_date else None
        observation_change = end['value'] - previous['value'] if previous else None
        if when > today:
            reason = 'future'
        elif not requested:
            reason = 'no_accounts'
        elif not end['complete']:
            reason = 'missing_value_or_fx'
        elif not start['complete']:
            reason = 'missing_previous_day'
        else:
            reason = None
        return {'date': when.isoformat(), 'start_value': start['value'], 'end_value': end['value'],
                'change': change, 'change_pct': change / start['value'] * 100 if complete and start['value'] > 0 else None,
                'return_pct': None, 'profit': None, 'external_flows': None,
                'observation_change': observation_change, 'observation_start_date': previous_date,
                'observation_start_value': previous['value'] if previous else None,
                'observation_change_pct': observation_change / previous['value'] * 100 if previous and previous['value'] > 0 else None,
                'observation_days': (when - date.fromisoformat(previous_date)).days if previous_date else None,
                'known_accounts': end['known_accounts'], 'selected_accounts': len(requested),
                'complete': complete, 'source': source, 'estimated': source == 'estimate', 'reason': reason}

    def period(first, last):
        observed_last = min(last, today)
        # A current period runs through its latest jointly observed date. An
        # unreported ongoing day must not erase an otherwise complete MTD/YTD
        # series; historical complete months still require their final day.
        if first <= today <= last:
            latest = next((when for when in reversed(list(_days(first, today))) if total(when)['complete']), None)
            if latest is not None:
                observed_last = latest
        cells = [cell(when) for when in _days(first, observed_last)]
        start = total(first - timedelta(days=1))
        end = total(observed_last) if observed_last >= first else {'value': None, 'complete': False}
        daily_complete = bool(cells) and all(c['complete'] for c in cells)
        complete = bool(cells) and start['complete'] and end['complete']
        change = end['value'] - start['value'] if complete else None
        return {'start_date': (first - timedelta(days=1)).isoformat(),
                'end_date': observed_last.isoformat() if cells else None,
                'start_value': start['value'], 'end_value': end['value'], 'change': change,
                'change_pct': change / start['value'] * 100 if complete and start['value'] > 0 else None,
                'return_pct': None, 'profit': None, 'external_flows': None,
                'complete': complete, 'daily_coverage_complete': daily_complete,
                'observed_days': sum(c['complete'] for c in cells),
                'expected_days': len(cells),
                'positive_days': sum(c['change'] is not None and c['change'] > 0 for c in cells),
                'negative_days': sum(c['change'] is not None and c['change'] < 0 for c in cells),
                'flat_days': sum(c['change'] == 0 for c in cells),
                'source': source, 'estimated': source == 'estimate'}

    first, last = date(year, month, 1), date(year, month, monthrange(year, month)[1])
    days = [cell(when) for when in _days(first, last)]
    months = [{'month': current, **period(date(year, current, 1), date(year, current, monthrange(year, current)[1]))}
              for current in range(1, 13)]
    summary = {'year': year, 'month': month, **months[month - 1]}
    years = sorted({int(day[:4]) for rows in data.values() for _, day in rows} | {today.year}, reverse=True)
    warnings = [
        'This calendar shows account-value change including deposits, withdrawals, transfers, fees, income and FX. It does not establish personal profit or investment return.',
    ]
    if source == 'estimate':
        warnings.append('SnapTrade historical account values are estimates and can differ from brokerage statements. They are shown separately from recorded snapshots.')
    if not summary['complete']:
        warnings.append('Monthly change is unavailable because the selected accounts do not have complete values and exchange rates at both period endpoints.')
    elif not summary['daily_coverage_complete']:
        warnings.append('Monthly value change compares the complete period endpoints. Some intervening daily changes are unavailable and remain blank.')
    if paper:
        warnings.append('Paper accounts contain simulated money and are shown separately from live accounts.')
    return {'currency': currency_code, 'year': year, 'month': month, 'source': source,
            'metric': 'account_value_change', 'selected_account_ids': requested, 'paper': paper,
            'available_years': years,
            'available_sources': [{'id': kind, 'label': label, 'points': len(data[kind])} for kind, label in SOURCES.items()],
            'days': days, 'months': months, 'summary': summary,
            'year_summary': {'year': year, **period(date(year, 1, 1), date(year, 12, 31))},
            'points': [total(when) for when in _days(first - timedelta(days=1), min(last, today))],
            'retrieved_at': cached.get('retrieved_at'), 'warnings': warnings,
            'methodology': [
                'A day compares the selected accounts on that exact date with the immediately preceding calendar date. Missing days are not filled or carried forward.',
                'Between-observation changes compare each known date with the previous date with complete selected-account values and FX. Multi-day intervals are labelled and assigned only to their ending date; they are not daily profit.',
                'All selected accounts and their dated exchange rates must be known at both endpoints. Current holdings are never used to reconstruct past holdings.',
                'Estimated account history and recorded snapshots are separate source choices. Recorded snapshots use the provider observation date when known, otherwise the saved snapshot date.',
                'Month and year value changes compare complete endpoint values even when intervening days are missing. Positive and negative day counts cover only comparable consecutive days.',
                'Profit, external flows and personal return remain unavailable because the activity feed does not prove complete historical deposits, withdrawals and transfers.',
            ]}

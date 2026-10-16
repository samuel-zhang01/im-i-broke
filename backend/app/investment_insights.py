"""Evidence-backed, cache-only review of connected investment observations.

A review flag describes a limitation or an observation, never a trading signal,
fraud verdict or reconstructed personal return. Cash flows, corporate actions,
fees and listing currencies are not silently inferred from price histories.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from statistics import median

from . import investing_analytics as analytics, investment_profile, market_cache, snaptrade_store
from .snaptrade_models import number

MIN_PRIOR_SESSIONS = 30
MAX_PRIOR_SESSIONS = 60
MAX_BASELINE_AGE_DAYS = 120
MIN_MOVE_PERCENT = 5.0
MODIFIED_Z_THRESHOLD = 3.5
CONCENTRATION_PERCENT = 20.0


def session_review(points, today):
    """Compare the latest return with prior returns, excluding it from baseline.

    The modified z-score uses a robust median/MAD scale. Zero MAD uses a 5pp
    deviation floor instead of division by zero. Missing sessions are not filled.
    A pair spanning more than one weekday is excluded conservatively: without a
    venue calendar we cannot distinguish a missing quote from a market holiday.
    """
    points = analytics._points({'points': points}, today)
    returns = []
    for first, last in zip(points, points[1:]):
        when = analytics._date(last['date'])
        gap = (when - analytics._date(first['date'])).days
        weekdays = sum((when - timedelta(days=offset)).weekday() < 5 for offset in range(min(gap, 8)))
        if gap <= 4 and weekdays == 1 and when >= today - timedelta(days=MAX_BASELINE_AGE_DAYS):
            returns.append({'date': last['date'], 'interval_days': gap,
                            'value': (last['close'] / first['close'] - 1) * 100})
    # Do not take an earlier valid pair when the latest pair has a long gap.
    if len(points) < 2 or not returns or returns[-1]['date'] != points[-1]['date']:
        return None
    prior = [r['value'] for r in returns[:-1]][-MAX_PRIOR_SESSIONS:]
    if len(prior) < MIN_PRIOR_SESSIONS:
        return None
    latest = returns[-1]['value']
    center = median(prior)
    mad = median(abs(value - center) for value in prior)
    deviation = abs(latest - center)
    threshold = max(MIN_MOVE_PERCENT, MODIFIED_Z_THRESHOLD * mad / .6744897501960817)
    return {'return_pct': latest, 'baseline_median_pct': center,
            'baseline_mad_pct': mad, 'deviation_threshold_pct': threshold,
            'sample_size': len(prior), 'as_of': returns[-1]['date'],
            'observed_interval_days': returns[-1]['interval_days'],
            'unusual': abs(latest) >= MIN_MOVE_PERCENT and deviation > threshold}


def _identity(position):
    expected = market_cache.listing(position)
    if expected is None:
        return None
    return (expected['symbol'], expected['currency'], tuple(sorted(expected['exchanges'])), expected['asset_type'])


def _evidence(label, value, unit='count', as_of=None):
    return {'label': label, 'value': value, 'unit': unit, 'as_of': as_of}


def _flag(code, category, title, detail, *, accounts=(), holdings=(), target='account',
          evidence=(), label='Inspect account', severity=None):
    account_ids = sorted(set(accounts))
    holding_ids = sorted({r['id'] for r in holdings})
    anchor = sorted(holdings, key=lambda r: r['id'])[0] if holdings else None
    # Provider array ordering can alter position IDs between snapshots. Verified
    # instrument + account identity keeps a recurring finding stable across it.
    issue_positions = sorted({r['account_id'] + '/' + (r.get('instrument_id') or r['position_id']) for r in holdings})
    identity = '|'.join([code, *account_ids, *issue_positions])
    return {'id': code + ':' + sha256(identity.encode()).hexdigest()[:16],
            'code': code, 'category': category,
            'severity': severity or ('warning' if category == 'quality' else 'info'),
            'title': title, 'detail': detail,
            'account_ids': account_ids, 'holding_ids': holding_ids,
            'action': {'label': label, 'target': target,
                       'account_id': anchor['account_id'] if anchor else next(iter(account_ids), None),
                       'position_id': anchor['position_id'] if anchor else None},
            'evidence': list(evidence)}


def bundle(conn, *, path=None, currency_code='GBP', account_ids=None, paper=False,
           today=None, cached=None, markets=None):
    today = today or datetime.now(timezone.utc).date()
    cached = snaptrade_store.load_portfolio(path=path) if cached is None else cached
    cached = cached or {'accounts': [], 'connections': {'data': []}, 'cache': {}}
    markets = market_cache.load_market_data(path=path) if markets is None else markets
    analysis = analytics.bundle(conn, path=path, currency_code=currency_code, account_ids=account_ids,
                                paper=paper, today=today, cached=cached, markets=markets)
    selected_ids = analysis['selected_account_ids']
    selected = [a for a in cached.get('accounts', []) if a['id'] in selected_ids]
    cards = {a['id']: a for a in analysis['accounts'] if a['selected']}
    holdings = [r for r in analysis['holdings'] if not r['is_cash']]
    raw = {(a['id'], p.get('id')): p for a in selected for p in a.get('positions', {}).get('data', [])}
    key_counts = Counter((a['id'], p.get('id')) for a in selected for p in a.get('positions', {}).get('data', []))
    duplicate_keys = {key for key, count in key_counts.items() if count > 1}
    items = []

    # Global identities matter even when a conflicting account is deselected.
    identities = defaultdict(set)
    for account in cached.get('accounts', []):
        for position in account.get('positions', {}).get('data', []):
            identity = _identity(position)
            if identity is not None:
                identities[position['instrument_id']].add(identity)
    conflicts = {identifier for identifier, values in identities.items() if len(values) > 1}
    verified_markets = analytics._consistent_markets(cached, markets)
    conflicting = [r for r in holdings if r['instrument_id'] in conflicts]
    if conflicting:
        items.append(_flag('instrument_identity_conflict', 'quality', 'Conflicting security identities',
            'The same provider instrument has different listing metadata across accounts. Its public price history and sector are withheld until the identity is resolved; positions remain separate.',
            accounts=[r['account_id'] for r in conflicting], holdings=conflicting, target='holding',
            label='Inspect holding', evidence=[_evidence('Affected positions', len(conflicting))]))

    for account in selected:
        card = cards[account['id']]
        section = account.get('positions', {})
        ids = [p.get('id') for p in section.get('data', [])]
        duplicate_extras = sum(count - 1 for count in Counter(ids).values() if count > 1)
        if duplicate_extras:
            items.append(_flag('duplicate_position_key', 'quality', 'Repeated holding identifiers',
                'More than one cached position has the same account and position identifier. Holding totals may be overstated; matching symbols in different accounts are not treated as duplicates.',
                accounts=[account['id']], evidence=[_evidence('Extra position rows', duplicate_extras)]))
        incomplete = section.get('status') not in {'ok', 'empty'} or section.get('truncated') is True
        missing_value = card['value'] is None
        if incomplete or missing_value:
            detail = []
            if missing_value:
                detail.append('Its account total or reporting-currency conversion is unavailable. The portfolio total contains known accounts only.')
            if incomplete:
                detail.append('Holdings are unsupported, unavailable or incomplete for this account; an empty list does not establish a zero balance.')
            items.append(_flag('account_coverage', 'quality', 'Incomplete account coverage', ' '.join(detail),
                accounts=[account['id']], evidence=[_evidence('Missing account total', int(missing_value)),
                _evidence('Incomplete holdings section', int(incomplete))]))
        if card['stale'] and section.get('data'):
            items.append(_flag('holdings_freshness', 'quality', 'Holdings snapshot needs checking',
                'This account has an older, incomplete or retained cached snapshot. Security-price returns may remain available, but current quantities do not establish a fresh daily account profit.',
                accounts=[account['id']], evidence=[_evidence('Snapshot age',
                    (today - analytics._date(card['holdings_as_of'])).days if analytics._date(card['holdings_as_of']) else None,
                    'days', card['holdings_as_of'])]))

    missing_cost = [r for r in holdings if r['quantity'] not in (None, 0)
                    and (r['native_average_cost'] is None or r['native_average_cost'] <= 0)]
    if missing_cost:
        items.append(_flag('cost_basis_context', 'quality', 'Cost basis needs context',
            'A missing or nonpositive cost basis cannot support a meaningful percentage gain. A zero basis can be legitimate for awards or transfers; check the broker record before interpreting the unrealized result.',
            accounts=[r['account_id'] for r in missing_cost], holdings=missing_cost, target='holding',
            label='Inspect holding', severity='info', evidence=[_evidence('Affected positions', len(missing_cost))]))
    missing_valuation = [r for r in holdings if r['value'] is None]
    if missing_valuation:
        items.append(_flag('holding_valuation', 'quality', 'Some holding values are unavailable',
            'Quantity, price, contract units or reporting-currency conversion is unavailable for these positions. Their missing values are excluded from the known securities subtotal, not treated as zero.',
            accounts=[r['account_id'] for r in missing_valuation], holdings=missing_valuation, target='holding',
            label='Inspect holding', evidence=[_evidence('Affected positions', len(missing_valuation))]))
    unavailable_prices = [r for r in holdings if r['market_status'] not in {'ready', 'unsupported'}
                          and r['instrument_id'] not in conflicts]
    if unavailable_prices:
        stale_count = sum(r['market_status'] == 'stale' for r in unavailable_prices)
        items.append(_flag('market_coverage', 'quality', 'Some price histories need checking',
            'Verified recent public prices are missing or retained after a refresh problem for these listings. Check each market date; weekends and market holidays alone do not imply a failed feed.',
            accounts=[r['account_id'] for r in unavailable_prices], holdings=unavailable_prices, target='holding',
            label='Inspect market dates', evidence=[_evidence('Affected positions', len(unavailable_prices)),
                                                    _evidence('Retained or older histories', stale_count)]))

    by_instrument = defaultdict(list)
    for row in holdings:
        if row['instrument_id'] and row['instrument_id'] not in conflicts:
            by_instrument[row['instrument_id']].append(row)
    eligible, large, unusual = 0, 0, 0
    for identifier, rows in by_instrument.items():
        if any((r['account_id'], r['position_id']) in duplicate_keys for r in rows):
            continue
        first = rows[0]
        position = raw[(first['account_id'], first['position_id'])]
        market = analytics._market(position, verified_markets, today)
        if market['status'] != 'ready':
            continue
        review = session_review(market['points'], today)
        if review is None:
            continue
        eligible += 1
        large += abs(review['return_pct']) >= MIN_MOVE_PERCENT
        if not review['unusual']:
            continue
        unusual += 1
        items.append(_flag('unusual_session_move', 'movement', 'Unusual observed price move',
            'The change between the latest pair of recorded closes is large relative to this listing’s own recent history. The latest session may still be open. It may be a genuine move or a corporate action; inspect the dated chart. This is not account profit or a trade recommendation.',
            accounts=[r['account_id'] for r in rows], holdings=rows, target='holding', label='Inspect price history',
            evidence=[_evidence('Latest observed price change', review['return_pct'], 'percent', review['as_of']),
                      _evidence('Prior-session median', review['baseline_median_pct'], 'percent'),
                      _evidence('Prior sessions compared', review['sample_size']),
                      _evidence('Required deviation from median', review['deviation_threshold_pct'], 'percent'),
                      _evidence('Latest observed interval', review['observed_interval_days'], 'days')]))

    # Gross absolute securities exposure avoids netting shorts into misleading
    # shares. The denominator is explicitly the KNOWN securities, never NAV.
    gross = sum(abs(r['value']) for r in holdings if r['value'] is not None)
    concentration = []
    for identifier, rows in by_instrument.items():
        if duplicate_keys or not gross or any(r['value'] is None or _identity(raw[(r['account_id'], r['position_id'])]) is None for r in rows):
            continue
        weight = sum(abs(r['value']) for r in rows) / gross * 100
        concentration.append(weight)
        if weight < CONCENTRATION_PERCENT:
            continue
        items.append(_flag('known_security_concentration', 'context', 'Large share of known securities',
            'This verified instrument represents at least 20% of the selected known gross securities exposure, combining its positions across accounts. Cash and unavailable holdings are excluded, so this is not a share of total wealth or a recommendation to change it.',
            accounts=[r['account_id'] for r in rows], holdings=rows, target='holding', label='Inspect holding',
            evidence=[_evidence('Share of known gross securities', weight, 'percent'),
                      _evidence('Accounts holding this instrument', len({r['account_id'] for r in rows}))]))

    history_gaps, history_accounts = [], 0
    for account in selected:
        dates = sorted({analytics._date(p.get('date')) for p in account.get('history', {}).get('data', [])
                        if analytics._date(p.get('date')) and analytics._date(p.get('date')) <= today
                        and number(p.get('total_value')) is not None})
        if dates:
            history_accounts += 1
        history_gaps.extend((last - first).days for first, last in zip(dates, dates[1:]))
    typical_interval = median(history_gaps) if history_gaps else None
    if typical_interval is not None and typical_interval > 1:
        items.append(_flag('history_cadence', 'context', 'History is sampled between observations',
            'Estimated account history does not contain every calendar day. Use “Between observations” for known intervals; missing daily changes stay blank. Value changes include cash flows and do not establish personal profit.',
            accounts=selected_ids, target='calendar', label='Inspect value calendar',
            evidence=[_evidence('Typical observation interval', typical_interval, 'days'),
                      _evidence('Observed intervals', len(history_gaps))]))
    items.sort(key=lambda item: ({'quality': 0, 'movement': 1, 'context': 2}[item['category']],
                                item['severity'] != 'warning', item['code'], item['id']))
    counts = Counter(item['category'] for item in items)
    result = {'as_of': today.isoformat(), 'retrieved_at': cached.get('retrieved_at'),
            'currency': analysis['currency'], 'selected_account_ids': selected_ids, 'paper': analysis['paper'],
            'summary': {'review_count': counts['quality'] + counts['movement'], 'quality_count': counts['quality'],
                        'movement_count': counts['movement'], 'context_count': counts['context'],
                        'accounts': len(selected), 'holdings': len(holdings)},
            'items': items,
            'coverage': {'accounts': len(selected), 'known_accounts': sum(c['value'] is not None for c in cards.values()),
                         'holdings': len(holdings), 'usable_cost_holdings': sum(r['native_average_cost'] is not None and r['native_average_cost'] > 0 for r in holdings),
                         'verified_market_holdings': sum(r['market_status'] == 'ready' for r in holdings),
                         'unique_instruments': len(by_instrument), 'movement_eligible_instruments': eligible,
                         'history_accounts': history_accounts, 'history_interval_days': typical_interval,
                         'activity_rows': sum(len(a.get('activities', {}).get('data', [])) for a in selected),
                         'personal_profit_available': False},
            'calibration': {'absolute_moves_at_least_5pct': large, 'unusual_session_moves': unusual,
                            'minimum_prior_sessions': MIN_PRIOR_SESSIONS,
                            'top1_known_gross_securities_pct': max(concentration, default=None),
                            'top5_known_gross_securities_pct': sum(sorted(concentration, reverse=True)[:5]) if concentration else None,
                            'multiaccount_instruments': sum(len({r['account_id'] for r in rows}) > 1 for rows in by_instrument.values())},
            'methodology': [
                'Review items use cached observations only. Checking this view makes no broker or market requests.',
                'Missing or unsupported data is not a zero balance. Shared tickers, suffixes or broker names are not duplicate-account evidence.',
                'Unusual moves use listing-currency price returns, at least 30 and at most 60 prior sessions within 120 calendar days, a modified median/MAD z-score above 3.5 and a 5 percentage-point minimum deviation. The latest observation is excluded from its own baseline.',
                'Price pairs spanning more than one weekday are excluded conservatively because no exchange holiday calendar is available. Friday-to-Monday observations can qualify; absent ordinary sessions are never filled.',
                'Price moves exclude dividends, fees, FX and your trades. A statistically unusual price is not proof of bad data; corporate actions can be legitimate.',
                'Concentration is a descriptive share of known gross absolute security values, excluding cash and unavailable holdings. Opposing positions are not netted. This is not a target allocation or investment advice.',
                'Estimated account-value history and a partial activity feed cannot certify cash flows, personal profit, time-weighted returns or money-weighted returns.',
            ]}
    result['data_profile'] = investment_profile.profile(cached, analysis, review=result, today=today)
    return result

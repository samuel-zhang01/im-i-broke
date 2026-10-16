"""Allowlisted investment quality aggregates; no identifiers or monetary values.

Inputs are normalized cached display records. This pure module does not open a
file, read credentials, contact a provider, or infer unseen account activity.
"""
from collections import Counter
from datetime import date, datetime, timezone
import math


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def day(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def quantiles(values):
    values = sorted(values)
    if not values:
        return {'count': 0, 'min': None, 'median': None, 'p90': None, 'max': None}
    def q(fraction):
        index = (len(values) - 1) * fraction
        low, high = math.floor(index), math.ceil(index)
        return round(values[low] + (values[high] - values[low]) * (index - low), 4)
    return {'count': len(values), 'min': q(0), 'median': q(.5), 'p90': q(.9), 'max': q(1)}


def frequencies(values):
    return dict(sorted(Counter(values).items()))


def profile(portfolio, analysis, calendar=None, review=None, *, today=None):
    today = today or datetime.now(timezone.utc).date()
    selected_ids = analysis.get('selected_account_ids')
    accounts = [a for a in portfolio.get('accounts', []) if (a['id'] in selected_ids if selected_ids is not None else not a.get('is_paper'))]
    positions = [(a, p) for a in accounts for p in a.get('positions', {}).get('data', [])]
    stocks = [(a, p) for a, p in positions if not p.get('is_cash')]
    holdings = [r for r in analysis.get('holdings', []) if not r.get('is_cash')]
    histories = [(a, p) for a in accounts for p in a.get('history', {}).get('data', [])]
    activities = [(a, p) for a in accounts for p in a.get('activities', {}).get('data', [])]
    ages = [(today - day(a.get('holdings_synced_at'))).days for a in accounts if day(a.get('holdings_synced_at'))]
    market_ages = [(today - day(r.get('market_as_of'))).days for r in holdings if day(r.get('market_as_of'))]
    gaps = []
    for a in accounts:
        days = sorted({day(p.get('date')) for p in a.get('history', {}).get('data', []) if day(p.get('date'))})
        gaps.extend((right - left).days for left, right in zip(days, days[1:]))
    suffix_groups = Counter((a.get('connection_id'), a.get('number_suffix')) for a in accounts if a.get('number_suffix'))
    instruments = {}
    for a, p in stocks:
        if p.get('instrument_id'):
            instruments.setdefault(p['instrument_id'], set()).add((p.get('raw_symbol'), p.get('exchange'), p.get('listing_currency'), p.get('asset_type')))
    gross = sum(abs(r['value']) for r in holdings if numeric(r.get('value')))
    by_instrument = {}
    instrument_accounts = {}
    for row in holdings:
        identifier = row.get('instrument_id')
        if identifier and numeric(row.get('value')) and len(instruments.get(identifier, [])) == 1:
            by_instrument[identifier] = by_instrument.get(identifier, 0) + abs(row['value'])
            instrument_accounts.setdefault(identifier, set()).add(row['account_id'])
    weights = sorted((value / gross * 100 for value in by_instrument.values()), reverse=True) if gross else []
    card_rows = {a['id']: a for a in analysis.get('accounts', []) if a.get('selected')}
    reconciliation = []
    for a in accounts:
        card = card_rows.get(a['id'], {})
        rows = [r for r in holdings if r['account_id'] == a['id']]
        if numeric(card.get('value')) and card['value'] > 0 and numeric(card.get('cash')) and all(numeric(r.get('value')) for r in rows):
            reconciliation.append(abs(card['value'] - card['cash'] - sum(r['value'] for r in rows)) / card['value'] * 100)
    def duplicate_extras(keys):
        counts = Counter(keys)
        return sum(count - 1 for count in counts.values() if count > 1)
    report = {
        'as_of': today.isoformat(), 'portfolio_retrieved_at': portfolio.get('retrieved_at'),
        'source': 'Am I Broke? normalized cached investment APIs; no provider refresh',
        'grain': {'accounts': 'provider account ID', 'positions': 'account ID + position ID',
                  'history': 'account ID + observation date + currency', 'activities': 'account ID + activity ID'},
        'accounts': {'live': sum(not a.get('is_paper') for a in accounts), 'paper': sum(bool(a.get('is_paper')) for a in accounts),
                     'missing_total': sum(not numeric(a.get('total_value')) for a in accounts),
                     'zero_total': sum(a.get('total_value') == 0 for a in accounts),
                     'negative_total': sum(numeric(a.get('total_value')) and a['total_value'] < 0 for a in accounts),
                     'holdings_count_distribution': quantiles([len(a.get('positions', {}).get('data', [])) for a in accounts]),
                     'holdings_age_calendar_days': quantiles(ages),
                     'same_connection_suffix_groups': sum(count > 1 for count in suffix_groups.values()),
                     'duplicate_account_id_extras': duplicate_extras(a.get('id') for a in accounts),
                     'section_statuses': {kind: frequencies(a.get(kind, {}).get('status', 'missing') for a in accounts)
                                          for kind in ('balances','positions','activities','history','return_rates')},
                     'truncated_sections': {kind: sum(bool(a.get(kind, {}).get('truncated')) for a in accounts)
                                            for kind in ('positions','activities','history')},
                     'stale_sections': {kind: sum(bool(a.get(kind, {}).get('stale')) for a in accounts)
                                        for kind in ('balances','positions','activities','history')},
                     'account_minus_holdings_and_cash_absolute_pct': quantiles(reconciliation)},
        'positions': {'all': len(positions), 'securities': len(stocks), 'cash_equivalents': len(positions) - len(stocks),
                      'asset_types': frequencies(p.get('asset_type') or 'unknown' for _, p in stocks),
                      'currencies': frequencies(p.get('currency') or 'unknown' for _, p in stocks),
                      'missing': {field: sum(p.get(field) is None or p.get(field) == '' for _, p in stocks)
                                  for field in ('id','instrument_id','raw_symbol','listing_currency','exchange','quantity','price','value','currency','average_cost','unrealized_pnl')},
                      'duplicate_account_position_extras': duplicate_extras((a.get('id'), p.get('id')) for a,p in stocks),
                      'instruments': len(instruments), 'conflicting_instrument_listings': sum(len(values) > 1 for values in instruments.values()),
                      'negative_quantity': sum(numeric(p.get('quantity')) and p['quantity'] < 0 for _,p in stocks),
                      'zero_quantity': sum(p.get('quantity') == 0 for _,p in stocks),
                      'nonpositive_cost': sum(numeric(p.get('average_cost')) and p['average_cost'] <= 0 for _,p in stocks),
                      'market_statuses': frequencies(r.get('market_status') or 'unknown' for r in holdings),
                      'market_age_calendar_days': quantiles(market_ages),
                      'missing_reporting_value': sum(r.get('value') is None for r in holdings),
                      'missing_day_move': sum(r.get('day_move_estimate') is None for r in holdings),
                      'sector_classified_direct': sum(r.get('sector') not in {None, 'Unclassified', 'Funds & ETFs'} for r in holdings),
                      'period_returns_available': {period: sum(numeric(r.get('returns', {}).get(period)) for r in holdings)
                                                    for period in ('1d','1w','1m','3m','ytd','1y')},
                      'absolute_latest_session_price_return_pct': quantiles([abs(r['returns']['1d']) for r in holdings if numeric(r.get('returns', {}).get('1d'))])},
        'concentration': {'basis': 'Known gross absolute security values; excludes cash and unavailable holdings',
                          'largest_instrument_pct': round(weights[0], 4) if weights else None,
                          'top5_instruments_pct': round(sum(weights[:5]), 4) if weights else None,
                          'instruments_at_least_20pct': sum(weight >= 20 for weight in weights),
                          'multiaccount_instruments': sum(len(accounts) > 1 for accounts in instrument_accounts.values())},
        'estimated_history': {'rows': len(histories),
                              'account_date_currency_duplicate_extras': duplicate_extras((a.get('id'), p.get('date'), p.get('currency')) for a,p in histories),
                              'observed_gap_calendar_days': quantiles(gaps),
                              'future_rows': sum(day(p.get('date')) > today for _,p in histories if day(p.get('date'))),
                              'missing_total': sum(not numeric(p.get('total_value')) for _,p in histories)},
        'activity': {'rows': len(activities),
                     'account_activity_duplicate_extras': duplicate_extras((a.get('id'), p.get('id')) for a,p in activities if p.get('id')),
                     'missing_ids': sum(not p.get('id') for _,p in activities),
                     'missing_currency': sum(not p.get('currency') for _,p in activities),
                     'missing_amount': sum(not numeric(p.get('amount')) for _,p in activities),
                     'types': frequencies(p.get('type') or 'unknown' for _,p in activities)},
    }
    if calendar:
        add_calendar_profile(report, calendar, today=today)
    if review:
        # Explicit allowlist: never copy item detail, identifiers or monetary data.
        report['review_calibration'] = {key: review.get('calibration', {}).get(key) for key in (
            'absolute_moves_at_least_5pct', 'unusual_session_moves', 'minimum_prior_sessions')}
        report['review_calibration']['eligible_own_history_instruments'] = review.get('coverage', {}).get('movement_eligible_instruments')
        report['review_calibration']['review_count'] = review.get('summary', {}).get('review_count')
        report['review_calibration']['context_count'] = review.get('summary', {}).get('context_count')
        report['review_calibration']['rule_counts'] = frequencies(item['code'] for item in review.get('items', []))
    return report



def add_calendar_profile(report, calendar, *, today=None):
    today = today or datetime.now(timezone.utc).date()
    days = [p for p in calendar.get('days', []) if (day(p.get('date')) or today) <= today]
    report['calendar'] = {'source': calendar.get('source'), 'elapsed_days': len(days),
                          'daily_changes_known': sum(p.get('change') is not None for p in days),
                          'between_observation_changes_known': sum(p.get('observation_change') is not None for p in days),
                          'observation_interval_days': quantiles([p['observation_days'] for p in days if p.get('observation_days')]),
                          'monthly_endpoints_known': calendar.get('summary', {}).get('complete') is True,
                          'personal_profit_available': any(p.get('profit') is not None for p in days)}
    return report

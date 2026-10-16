"""Daily investment valuation from dated shares, cash, prices and reference FX.

Pure read projection: source rows are never changed and no providers are called.
Statement account identity remains separate from a live account until an explicit
source link exists. Current shares are used only to validate an export endpoint,
never as a substitute for historical quantities.
"""
from bisect import bisect_right
from datetime import date, timedelta
import json
import math

from .market_cache import listing
from .portfolio import major_currency
from .historical_listings import LISTINGS

EPS = 1e-6


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _meta(value):
    try:
        value = json.loads(value or '{}')
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _day(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _same(left, right, tolerance=EPS):
    return all(abs(left.get(key, 0) - right.get(key, 0)) <= tolerance for key in left.keys() | right.keys())


def _amounts(rows, key):
    result = {}
    for row in rows:
        identity, quantity = row.get('isin'), _number(row.get(key))
        if not identity or quantity is None or identity in result:
            return None
        result[identity] = quantity
    return result


def _cash(raw):
    if not isinstance(raw, dict):
        return None
    result = {key: _number(value) for key, value in raw.items()}
    return result if all(value is not None for value in result.values()) else None


def _events(conn, account):
    result = []
    for row in conn.execute('SELECT id,txn_date,type_code,meta FROM transactions WHERE account_id=?', (account,)):
        meta = _meta(row['meta'])
        when = _day(meta.get('time') or row['txn_date'])
        if when is None:
            return None
        result.append({'date': when, 'sort': str(meta.get('time') or row['txn_date']),
                       'id': row['id'], 'type': row['type_code'], 'meta': meta})
    return sorted(result, key=lambda row: (row['date'], row['sort'], row['id']))


def _statement_scopes(event):
    meta = event['meta']
    observations = meta.get('source_observations') or []
    if not isinstance(observations, list):
        observations = []
    scopes = set()
    for source in [meta, *observations]:
        if not isinstance(source, dict):
            continue
        start, end = _day(source.get('source_statement_start')), _day(source.get('source_statement_end'))
        if start is not None and end is not None and start <= end:
            scopes.add((start, end))
    return scopes


def _statement_event(event, start, end):
    scopes = _statement_scopes(event)
    if (start, end) in scopes:
        return True
    # Once an end date has explicit scopes, it cannot identify another daily
    # or monthly interval ending on that same date. Older imports retain their
    # original end-only assignment until scoped provenance is available.
    return (not any(scope_end == end for _, scope_end in scopes)
            and _day(event['meta'].get('source_statement_end')) == end)


def _apply(quantities, cash, event):
    meta = event['meta']
    movements = _cash(meta.get('cash_movements'))
    if movements is None:
        return False
    quantity = _number(meta.get('shares'))
    if meta.get('shares') is not None and quantity is None:
        return False
    if quantity:
        identity = meta.get('isin')
        if not identity:
            return False
        if event.get('ratio'):
            quantities[identity] = quantities.get(identity, 0) * event['ratio']
        else:
            quantities[identity] = quantities.get(identity, 0) + quantity
        if quantities[identity] < -EPS:
            return False
    for code, amount in movements.items():
        cash[code] = cash.get(code, 0) + amount
    return True


def _instruments(rows, cached, markets):
    """Match exact ticker, listing currency and verified venue; reject ambiguity."""
    positions = [position for account in (cached or {}).get('accounts', [])
                 for position in account.get('positions', {}).get('data', [])]
    result = {}
    for identity, info in rows.items():
        symbol, code = info.get('ticker'), info.get('currency')
        candidates = {}
        historical = [row for row in LISTINGS if row['isin'] == identity
                      and row['ticker'] == symbol and row['currency'] == code]
        for position in positions + historical:
            if position.get('raw_symbol') != symbol or position.get('listing_currency') != code:
                continue
            expected = listing(position)
            market = markets.get(position.get('instrument_id'))
            if not expected or not market or market.get('status') != 'ok':
                continue
            if (market.get('symbol') != expected['symbol'] or market.get('currency') != code
                    or market.get('exchange') not in expected['exchanges']):
                continue
            if info.get('exchange') == 'US' and expected['symbol'] != symbol.replace('.', '-'):
                continue
            candidates[position['instrument_id']] = market
        # The same verified listing can occur under separate provider identities.
        signatures = {(market['symbol'], market['currency'], market['exchange']) for market in candidates.values()}
        if len(signatures) == 1:
            chosen = next(iter(candidates.values()))
            points = sorted([point for point in chosen.get('points', [])
                             if _day(point.get('date')) and (_number(point.get('close')) or 0) > 0],
                            key=lambda point: point['date'])
            result[identity] = {**chosen, '_points': points, '_dates': [point['date'] for point in points]}
    return result


def _price(market, when):
    if not market:
        return None
    points = market.get('_points', market.get('points', []))
    index = bisect_right(market['_dates'] if '_dates' in market else [point['date'] for point in points], when.isoformat()) - 1
    if index < 0 or (when - _day(points[index]['date'])).days > 7:
        return None
    amount = float(points[index]['close'])
    if market.get('split_adjusted'):
        if market.get('split_events_complete') is not True or not isinstance(market.get('splits'), list):
            return None
        # Close is in today's split-adjusted units. Restore the units that the
        # actual historical ledger held on this date before multiplying.
        for split in market['splits']:
            split_day = _day(split.get('date'))
            numerator, denominator = _number(split.get('numerator')), _number(split.get('denominator'))
            if split_day is None or not numerator or not denominator or numerator <= 0 or denominator <= 0:
                return None
            if split_day > when:
                amount *= numerator / denominator
    return amount


def _observation(when, quantities, cash, identities, instruments, rates):
    total, missing = 0., []
    held = [(identity, quantity) for identity, quantity in quantities.items() if abs(quantity) > EPS]
    for identity, quantity in held:
        market = instruments.get(identity)
        price = _price(market, when)
        value = rates.convert(price * quantity if price is not None else None,
                              market.get('currency') if market else None, 'GBP', when.isoformat())
        if value is None:
            missing.append(identities.get(identity, {}).get('ticker') or 'Unidentified holding')
        else:
            total += value
    cash_missing = []
    for code, amount in cash.items():
        if abs(amount) <= EPS:
            continue
        value = rates.convert(amount, code, 'GBP', when.isoformat())
        if value is None:
            cash_missing.append(code)
        else:
            total += value
    return {'date': when.isoformat(), 'value': round(total, 2) if not missing and not cash_missing else None,
            'currency': 'GBP', 'source': 'reconstructed', 'valuation_basis': 'daily_holdings',
            'estimated': True, 'reconciled': not missing and not cash_missing,
            'holdings_count': len(held), 'priced_holdings': len(held) - len(missing),
            'missing_prices_or_fx': missing, 'missing_cash_fx': cash_missing}


def _series(start, end, quantities, cash, events, identities, cached, markets, rates, cutoff):
    instruments = _instruments(identities, cached, markets)
    quantities, cash = dict(quantities), dict(cash)
    observations, event_index = [], 0
    when = start
    while when <= end:
        while event_index < len(events) and events[event_index]['date'] <= when:
            if not _apply(quantities, cash, events[event_index]):
                return [], ['Dated events do not establish nonnegative holding quantities and complete cash movements.']
            event_index += 1
        if when >= cutoff:
            observations.append(_observation(when, quantities, cash, identities, instruments, rates))
        when += timedelta(days=1)
    missing = sorted({symbol for point in observations for symbol in point['missing_prices_or_fx']})
    issues = ['Daily valuation is unavailable for holdings without verified prices, split evidence or dated FX: ' + ', '.join(missing)] if missing else []
    if any(point['missing_cash_fx'] for point in observations):
        issues.append('Some cash currencies lack dated FX; those daily totals are withheld.')
    return observations, issues


def _moomoo(statements, events, *, cached, markets, rates, today, cutoff):
    observations, issues, periods, cash_timing = {}, [], 0, []
    daily_periods = [(start, end) for row in statements
                     if _meta(row['meta']).get('statement_type') == 'daily'
                     for start, end in [(_day(row['period_start']), _day(row['period_end']))]
                     if start is not None and end is not None]
    for statement in statements:
        meta = _meta(statement['meta'])
        start, end = _day(statement['period_start']), _day(meta.get('snapshot_date') or statement['period_end'])
        if not start or not end or end < cutoff or start > today:
            continue
        label = end.isoformat()
        opening = _amounts(meta.get('position_changes', []), 'starting_shares')
        closing = _amounts(meta.get('positions', []), 'shares')
        opening_cash, closing_cash = _cash(meta.get('opening_cash_balances')), _cash(meta.get('cash_balances'))
        if meta.get('native_reconciled') is not True or any(value is None for value in (opening, closing, opening_cash, closing_cash)):
            issues.append(f'{label}: independent opening and closing statement evidence is incomplete.')
            continue
        if abs(_number(meta.get('assets_in_transit')) or 0) > EPS:
            issues.append(f'{label}: assets in transit lack dated daily valuation evidence.')
            continue
        selected = [event for event in events if _statement_event(event, start, end)]
        if any(start <= event['date'] <= end
               and _day(event['meta'].get('source_statement_end')) is None and not _statement_scopes(event)
               for event in events):
            issues.append(f'{label}: events without a statement assignment prevent a complete period replay.')
            continue
        outside = [event for event in selected if event['date'] < start or event['date'] > end]
        next_weekday = end + timedelta(days=1)
        while next_weekday.weekday() >= 5:
            next_weekday += timedelta(days=1)
        # These broker statements include a dividend and its withholding in
        # closing cash while printing the posting timestamp on the next weekday.
        # The independently reconciled closing balance proves recognition by
        # the statement date, not the exact earlier economic entitlement date.
        # Recognize these CASH-ONLY entries at that boundary in the estimated
        # valuation, keeping all original timestamps and event metadata intact.
        # Trades, transfers, earlier postings and longer timing gaps are never
        # shifted to manufacture a reconciled ownership history.
        allowed_cash_actions = {'Dividend', 'Dividend withholding tax'}
        if any(event['date'] != next_weekday or event['type'] not in allowed_cash_actions
               or _number(event['meta'].get('shares')) not in (None, 0)
               or (event['meta'].get('shares') is not None and _number(event['meta']['shares']) is None)
               for event in outside):
            issues.append(f'{label}: events outside the statement period lack supported cash-only recognition timing.')
            continue
        adjusted = [{'statement_date': label, 'posted_date': event['date'].isoformat(),
                     'action': event['type'], 'cash_movements': dict(event['meta'].get('cash_movements') or {})}
                    for event in outside]
        replay_events = [{**event, 'date': end} if event['date'] > end else event for event in selected]
        replay_events.sort(key=lambda event: (event['date'], event['sort'], event['id']))
        quantities, cash = dict(opening), dict(opening_cash)
        if not all(_apply(quantities, cash, event) for event in replay_events) or not _same(quantities, closing) or not _same(cash, closing_cash, .011):
            issues.append(f'{label}: events do not reconcile to the statement closing quantities and cash.')
            continue
        identities = {row['isin']: {'ticker': row.get('ticker'), 'currency': row.get('currency'), 'exchange': row.get('exchange')}
                      for row in meta.get('position_changes', []) + meta.get('positions', []) if row.get('isin')}
        for event in selected:
            item = event['meta']
            if item.get('isin') and item.get('shares'):
                identities.setdefault(item['isin'], {'ticker': item.get('ticker'), 'currency': item.get('execution_currency_major') or item.get('native_currency'), 'exchange': item.get('exchange')})
        values, notes = _series(start, min(end, today), opening, opening_cash, replay_events, identities, cached, markets, rates, cutoff)
        is_daily = meta.get('statement_type') == 'daily'
        overlaps_daily = not is_daily and any(daily_start <= end and daily_end >= start
                                               for daily_start, daily_end in daily_periods)
        for value in values:
            value['cash_timing_estimated'] = bool(adjusted)
            value['source_statement_type'] = 'daily' if is_daily else 'monthly'
            value['overlaps_daily_reports'] = overlaps_daily
            old = observations.get(value['date'])
            # Two independently reconciled reports can differ by a few pence
            # when their printed cash is rounded separately. Keep the narrower
            # daily replay within one reporting pound; a larger conflict is
            # withheld rather than silently replaced by the monthly estimate.
            if old and old['value'] is not None and value['value'] is not None and abs(old['value'] - value['value']) <= 1:
                if is_daily and old.get('source_statement_type') != 'daily':
                    observations[value['date']] = value
            elif old and old['value'] != value['value']:
                observations[value['date']] = {**value, 'value': None, 'reconciled': False}
                issues.append(f'{label}: overlapping statement periods disagree.')
            else:
                observations[value['date']] = value
        issues.extend(notes)
        if adjusted:
            cash_timing.extend(adjusted)
            issues.append(f'{label}: dividend cash and withholding posted on {next_weekday.isoformat()} '
                          'are already included in reconciled statement closing cash. The daily estimate recognizes '
                          'them on the statement date; the exact earlier economic recognition date is not established.')
        periods += 1
    return list(observations.values()), issues, {'reconciled_periods': periods, 'cash_reconciled': True,
        'cash_timing_estimated_periods': len({item['statement_date'] for item in cash_timing}),
        'cash_timing_adjustments': cash_timing}


def _saved_endpoints(conn, end):
    """Reuse durable dated quantity evidence when the live portfolio changes."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'snaptrade_account_observations', 'snaptrade_position_observations'}.issubset(tables):
        return []
    position_columns = {row[1] for row in conn.execute('PRAGMA table_info(snaptrade_position_observations)')}
    account_columns = {row[1] for row in conn.execute('PRAGMA table_info(snaptrade_account_observations)')}
    if not {'raw_symbol', 'listing_currency', 'instrument_id'}.issubset(position_columns) or 'holdings_truncated' not in account_columns:
        return []
    # Account totals and share snapshots can have different source clocks.
    # Old rows used observed_at for holdings; new rows use it for the balance.
    legacy_clock = "CASE WHEN source_time_known=1" + (
        " AND total_value_as_of IS NULL" if 'total_value_as_of' in account_columns else '') + " THEN observed_at END"
    quantity_clock = (f'COALESCE(holdings_observed_at, {legacy_clock})'
                      if 'holdings_observed_at' in account_columns else legacy_clock)
    result = []
    for row in conn.execute(f'''SELECT *, {quantity_clock} AS quantity_observed_at FROM snaptrade_account_observations
        WHERE {quantity_clock}>=? AND {quantity_clock}<? AND holdings_status IN ('ok','empty')
        AND holdings_truncated=0 ORDER BY {quantity_clock}''',
        (end.isoformat(), (end + timedelta(days=8)).isoformat())):
        positions = [dict(position) for position in conn.execute('''SELECT * FROM snaptrade_position_observations
            WHERE snapshot_id=? AND account_id=?''', (row['snapshot_id'], row['account_id']))]
        if row['holdings_status'] == 'ok' and not positions:
            continue  # A retained stale section without saved rows is not an empty account.
        result.append({'id': row['account_id'], 'institution': row['institution'], 'is_paper': bool(row['is_paper']),
                       'holdings_synced_at': row['quantity_observed_at'], 'positions': {'status': row['holdings_status'], 'data': positions},
                       'activities': {'status': 'empty', 'data': []}, 'balances': {'status': 'unsupported', 'data': []},
                       'saved_quantity_endpoint': True})
    return result


def _t212(conn, account_id, events, statements, *, cached, markets, rates, today, cutoff):
    """Validate exported shares against an independent current broker endpoint."""
    if not events or events[0]['type'] != 'Deposit':
        return [], ['An opening cash deposit and complete export are needed to establish this account history.'], {}
    end = max((_day(row['period_end']) for row in statements), default=None)
    if end is None:
        return [], ['Export coverage has no valid ending date.'], {}
    if any(event['date'] > end for event in events):
        return [], ['Source events extend beyond the declared export ending date.'], {}
    identities = {}
    for event in events:
        meta = event['meta']
        if meta.get('isin') and meta.get('shares'):
            code, _ = major_currency(meta.get('execution_currency_major') or meta.get('execution_price_currency') or meta.get('native_currency'))
            identities[meta['isin']] = {'ticker': meta.get('ticker'), 'currency': code}
    # Independent, effective-dated split evidence must be applied once.
    has_actions = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='corporate_actions'").fetchone()
    if has_actions:
        printed = {(event['meta'].get('isin'), event['date']) for event in events if event['type'] == 'Stock split'}
        for row in conn.execute('SELECT isin,ex_date,ratio_new,ratio_old FROM corporate_actions WHERE account_id=?', (account_id,)):
            when = _day(row['ex_date'])
            if when and when <= end and row['isin'] in identities and (row['isin'], when) not in printed:
                events.append({'date': when, 'sort': when.isoformat(), 'id': -1, 'type': 'Stock split',
                               'ratio': float(row['ratio_new']) / float(row['ratio_old']),
                               'meta': {'isin': row['isin'], 'shares': 1, 'cash_movements': {}}})
    events.sort(key=lambda event: (event['date'], event['sort'], event['id']))
    quantities, cash = {}, {}
    for event in events:
        if not _apply(quantities, cash, event) or any(value < -.02 for value in cash.values()):
            return [], ['Export share/cash replay is incomplete or has an unsupported negative holding or cash balance.'], {}
    matches = {}
    # Current candidates win over saved snapshots of the same account. Multiple
    # snapshots of one account do not create artificial matching ambiguity.
    candidates = list((cached or {}).get('accounts', [])) + _saved_endpoints(conn, end)
    for account in candidates:
        if 'trading212' not in str(account.get('institution', '')).lower().replace(' ', '') or account.get('is_paper'):
            continue
        positions = account.get('positions', {})
        activity = account.get('activities', {})
        if positions.get('status') not in {'ok', 'empty'} or positions.get('truncated') or positions.get('stale'):
            continue
        if activity.get('status') not in {'ok', 'empty'} or activity.get('truncated') or activity.get('stale'):
            continue
        # Later trades can change the independent quantity endpoint. Refuse to
        # assume an activity feed covers an arbitrarily old statement ending.
        observed = _day(account.get('holdings_synced_at'))
        if observed is None or not 0 <= (observed - end).days <= 7:
            continue
        later_trades = [row for row in activity.get('data', []) if end < (_day(row.get('date')) or end) <= observed
                        and row.get('type') in {'BUY', 'SELL', 'TRANSFER', 'REI', 'SPLIT'}]
        if later_trades:
            continue
        actual = {(row.get('raw_symbol'), row.get('listing_currency')): _number(row.get('quantity'))
                  for row in positions.get('data', []) if not row.get('is_cash') and abs(_number(row.get('quantity')) or 0) > EPS}
        expected = {(identities[key]['ticker'], identities[key]['currency']): value
                    for key, value in quantities.items() if abs(value) > EPS and key in identities}
        if any(value is None for value in actual.values()) or not _same(expected, actual):
            continue
        matches.setdefault(account['id'], (account, len(expected), observed))
    if len(matches) != 1:
        return [], ['Export closing quantities do not uniquely reconcile to a recent independent connected account.'], {'quantities_reconciled': False}
    account, matched_holdings, observed = next(iter(matches.values()))
    balance = account.get('balances', {})
    actual_cash = {row['currency']: row['cash'] for row in balance.get('data', []) if row.get('currency') and _number(row.get('cash')) is not None}
    cash_known = balance.get('status') in {'ok', 'empty'} and not balance.get('truncated') and not balance.get('stale')
    residual = {code: round(cash.get(code, 0) - actual_cash.get(code, 0), 8) for code in cash.keys() | actual_cash.keys()
                if abs(cash.get(code, 0) - actual_cash.get(code, 0)) > .011} if cash_known else None
    identity_cache = {'accounts': list((cached or {}).get('accounts', [])) + [account]}
    values, issues = _series(events[0]['date'], min(end, today), {}, {}, events, identities, identity_cache, markets, rates, cutoff)
    if residual is None or residual:
        issues.append('Exported cash differs from the current broker cash balance; historical cash is an estimate and excludes unrecorded post-export events.')
    for value in values:
        value['cash_reconciled'] = cash_known and not residual
    return values, issues, {'quantities_reconciled': True, 'reconciled_holdings': matched_holdings,
                           # Quantity agreement identifies an inspection candidate,
                           # not a verified identity link between these accounts.
                           'quantity_matched_account_id': account['id'],
                           'cash_reconciled': cash_known and not residual, 'cash_residual': residual,
                           'export_end': end.isoformat(), 'quantity_check_date': observed.isoformat(),
                           'saved_quantity_endpoint': account.get('saved_quantity_endpoint', False),
                           'identity_link_required': True}


def reconstruct(conn, *, cached, markets, rates, today=None, cutoff=None):
    """Return per-statement-account observations and explicit evidence coverage."""
    today = today or date.today()
    cutoff = _day(cutoff) if cutoff is not None else today - timedelta(days=365 * 2)
    cutoff = cutoff or today - timedelta(days=365 * 2)
    result = {'sources': {}, 'issues': []}
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'accounts', 'statements', 'transactions'}.issubset(tables):
        return result
    for account in conn.execute("SELECT id,bank FROM accounts WHERE account_type='investment'"):
        statements = [dict(row) for row in conn.execute('SELECT period_start,period_end,meta FROM statements WHERE account_id=? ORDER BY period_end,id', (account['id'],))]
        events = _events(conn, account['id'])
        source_id = 'statement:' + account['id']
        args = dict(cached=cached, markets=markets, rates=rates, today=today, cutoff=cutoff)
        if events is None:
            values, issues, coverage = [], ['Some source activity has no valid date.'], {}
        elif statements and all(_meta(row['meta']).get('parser') == 'moomoo' for row in statements):
            values, issues, coverage = _moomoo(statements, events, **args)
        elif 'trading212' in account['bank'].lower().replace(' ', ''):
            values, issues, coverage = _t212(conn, account['id'], events, statements, **args)
        else:
            continue
        values.sort(key=lambda row: row['date'])
        valid = [row for row in values if row['value'] is not None]
        coverage.update(days=len(values), valued_days=len(valid),
                        start_date=valid[0]['date'] if valid else None, end_date=valid[-1]['date'] if valid else None)
        result['sources'][source_id] = {'observations': values, 'coverage': coverage, 'issues': list(dict.fromkeys(issues))}
    return result

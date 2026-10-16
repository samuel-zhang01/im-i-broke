"""Cached investing analysis with explicit price-return and valuation semantics.

Broker balances are the account value, including cash. Public market history
describes a security's price performance; it is never an account return or a
reconstruction of the user's historic trades. This module performs no network IO.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from . import snaptrade_store, wealth
from .snaptrade_models import currency, number
from .market_cache import missing_previous_close


RANGES = {'1w': 7, '1m': 30, '3m': 90, '6m': 180, 'ytd': None, '1y': 365, 'all': None}
RETURN_RANGES = ('1d', '1w', '1m', '3m', 'ytd', '1y')
ORDINARY_ASSETS = {'stock', 'etf', 'mutualfund', 'mutual_fund', 'cef', 'adr', 'crypto'}
FUND_ASSETS = {'etf', 'mutualfund', 'mutual_fund', 'cef'}


def _sum(values):
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def _earliest_timestamp(values):
    """A cohort timestamp is known only if every included source is dated."""
    dated = []
    for raw in values:
        try:
            parsed = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
        except (ValueError, TypeError):
            return None
        parsed = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
        dated.append((parsed.timestamp(), raw))
    return min(dated, key=lambda item: item[0])[1] if dated else None


def _percent(end, start):
    return (end / start - 1) * 100 if end is not None and start is not None and start > 0 else None


def _date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _old(value, today, days):
    parsed = _date(value)
    return parsed is None or parsed > today or (today - parsed).days > days


def _verified_saved_positions(conn, account):
    """Prove retained quantities came from a complete successful observation.

    A section refresh error is not the age of its saved data. Legacy caches do
    not reliably preserve the earlier truncation flag, so a recent timestamp
    alone cannot certify completeness. Read the original archive without
    promoting retained rows to a new observation or changing their source time.
    """
    section = account.get('positions', {})
    if (section.get('status') != 'error' or not section.get('stale')
            or section.get('truncated') or not section.get('as_of') or not section.get('cached_at')):
        return False
    required = {
        'snaptrade_account_observations': {'holdings_observed_at', 'holdings_truncated'},
        'snaptrade_position_observations': {'instrument_id'},
    }
    for table, columns in required.items():
        if not columns <= {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}:
            return False
    source = conn.execute('''SELECT snapshot_id, holdings_observed_at
        FROM snaptrade_account_observations
        WHERE account_id=? AND retrieved_at=? AND holdings_status='ok' AND holdings_truncated=0''',
        (account['id'], section['cached_at'])).fetchone()
    if source is None or source['holdings_observed_at'] != section['as_of']:
        return False
    archived = conn.execute('''SELECT position_id AS id,instrument_id,quantity,is_cash
        FROM snaptrade_position_observations WHERE snapshot_id=? AND account_id=?''',
        (source['snapshot_id'], account['id'])).fetchall()
    current = section.get('data', [])
    def identity(row):
        return row['id'], row['instrument_id'], row['quantity'], bool(row['is_cash'])
    # Count plus identity equality also rejects duplicate rows and partial merges.
    expected = {identity(row) for row in archived}
    observed = {(row.get('id'), row.get('instrument_id'), number(row.get('quantity')),
                 row.get('is_cash') is True) for row in current}
    return bool(expected) and len(expected) == len(archived) == len(current) and expected == observed


def _points(market, today):
    """Reject bad/ambiguous days instead of choosing one conflicting close."""
    values, conflicts = {}, set()
    for raw in market.get('points', []):
        if not isinstance(raw, dict):
            continue
        parsed, close = _date(raw.get('date')), number(raw.get('close'))
        if parsed is None or parsed > today or close is None or close <= 0:
            continue
        day = parsed.isoformat()
        if day in values and values[day] != close:
            conflicts.add(day)
        values[day] = close
    return [{'date': day, 'close': values[day]} for day in sorted(values) if day not in conflicts]


def _cutoff(range_name, today):
    if range_name == 'ytd':
        return date(today.year, 1, 1) - timedelta(days=1)
    return today - timedelta(days=RANGES[range_name]) if RANGES[range_name] else None


def _anchor(points, target):
    index = bisect_right([p['date'] for p in points], target.isoformat()) - 1
    if index < 0 or (target - _date(points[index]['date'])).days > 7:
        return None
    return points[index]


def _return(points, period, today):
    if len(points) < 2:
        return None
    latest = points[-1]
    if period == '1d':
        last_date = _date(latest['date'])
        gap = (last_date - _date(points[-2]['date'])).days
        # Without a venue calendar, only consecutive weekdays (including
        # Friday to Monday) establish a single session. A missing close or an
        # unverified holiday must not turn several sessions into a daily move.
        weekdays = sum((last_date - timedelta(days=offset)).weekday() < 5
                       for offset in range(min(gap, 5)))
        if not 1 <= gap <= 4 or weekdays != 1:
            return None
        first = points[-2]
    elif period == 'all':
        first = points[0]
    else:
        first = _anchor(points, _cutoff(period, today))
    if not first or first['date'] == latest['date']:
        return None
    return _percent(latest['close'], first['close'])


def _split_since_holdings(market, holdings_as_of, today):
    observed = _date(holdings_as_of)
    if observed is None:
        return False  # The independent freshness check already withholds it.
    # Daily split metadata does not establish the event's exact time. A
    # same-day holdings observation therefore cannot prove post-split units.
    return any(observed <= when <= today for split in (market.get('splits') or [])
               if isinstance(split, dict) and (when := _date(split.get('date'))) is not None)


def _market(position, markets, today):
    # Provider identity, never ticker alone: identical symbols can represent
    # different exchanges, currencies or securities.
    instrument_id = position.get('instrument_id')
    raw = markets.get(instrument_id, {}) if instrument_id else {}
    if not isinstance(raw, dict):
        raw = {}
    # An instrument ID does not excuse conflicting listing metadata after a
    # broker migration, ticker change or bad upstream identity assignment.
    from .market_cache import listing
    expected = listing(position)
    if raw and (not expected or raw.get('symbol') != expected['symbol']
                or raw.get('currency') != expected['currency']
                or raw.get('exchange') not in expected['exchanges']):
        raw = {}
    points = _points(raw, today)
    last_day = points[-1]['date'] if points else None
    as_of = raw.get('as_of') if _date(raw.get('as_of')) and _date(raw.get('as_of')) <= today else last_day
    code = currency(raw.get('currency'))
    supported = position.get('asset_type') in ORDINARY_ASSETS and not position.get('is_cash')
    if not supported:
        status = 'unsupported'
    elif not raw:
        status = 'unmatched'
    elif not points or not code:
        status = 'no_history'
    elif _old(last_day, today, 5) or raw.get('stale') or raw.get('error') or raw.get('status') == 'error':
        status = 'stale'
    else:
        status = 'ready'
    sector = raw.get('sector') if isinstance(raw.get('sector'), str) else None
    if position.get('is_cash'):
        sector = 'Cash equivalents'
    elif position.get('asset_type') in FUND_ASSETS:
        # A fund issuer's industry is not a look-through classification.
        sector = 'Funds & ETFs'
    else:
        sector = sector or 'Unclassified'
    return {**raw, 'points': points, 'as_of': as_of, 'currency': code,
            'status': status, 'sector': sector}


def _cash(account, rates, code, today):
    balances = account.get('balances', {})
    values = [rates.convert(number(row.get('cash')), currency(row.get('currency')), code, today)
              for row in balances.get('data', [])]
    return _sum(values)


def _load_markets(path):
    # Imported lazily so the pure calculations remain usable in isolation.
    from . import market_cache
    return market_cache.load_market_data(path=path)


def _consistent_markets(cached, markets):
    """Withhold a shared history if current accounts disagree on its listing."""
    from .market_cache import listing
    seen, conflicts = {}, set()
    for account in cached.get('accounts', []):
        for position in account.get('positions', {}).get('data', []):
            expected = listing(position)
            if expected:
                identifier = expected['instrument_id']
                if identifier in seen and seen[identifier] != expected:
                    conflicts.add(identifier)
                seen[identifier] = expected
    return {key: value for key, value in markets.items() if key not in conflicts} if conflicts else markets


def bundle(conn, *, path=None, currency_code='GBP', range_name='1m', account_ids=None,
           paper=False, today=None, markets=None, cached=None):
    today = today or datetime.now(timezone.utc).date()
    if not currency(currency_code) or range_name not in RANGES:
        raise ValueError('Choose a supported currency and date range.')
    cached = snaptrade_store.load_portfolio(path=path) if cached is None else cached
    cached = cached or {'accounts': [], 'connections': {'data': []}, 'cache': {}}
    raw_accounts = cached.get('accounts', [])
    requested = snaptrade_store.selected_account_ids(raw_accounts, account_ids, paper=paper)
    selected = [a for a in raw_accounts if a['id'] in requested]
    modes = {bool(a.get('is_paper')) for a in selected}
    if len(modes) > 1:
        raise ValueError('View live and paper accounts separately.')
    paper = next(iter(modes)) if modes else paper
    rates = wealth.Rates(conn)
    markets = _load_markets(path) if markets is None else markets
    markets = _consistent_markets(cached, markets)
    disabled = {c['id'] for c in cached.get('connections', {}).get('data', []) if c.get('disabled')}
    today_text = today.isoformat()
    accounts, holdings, split_affected = [], [], set()
    for account in raw_accounts:
        positions = account.get('positions', {})
        holdings_as_of = positions.get('as_of') or account.get('holdings_synced_at')
        source_stale = (account.get('connection_id') in disabled
                        or account.get('initial_sync_complete') is False
                        or _old(holdings_as_of, today, 2))
        saved_positions = not source_stale and _verified_saved_positions(conn, account)
        complete_positions = (not positions.get('stale') and not positions.get('truncated')
                              and positions.get('status') in {'ok', 'empty'}) or saved_positions
        # Overall cache health includes unrelated accounts and transaction feeds.
        # Only this account's proven quantities and source clock govern its move.
        stale = bool(source_stale or not complete_positions)
        rows = []
        for position in positions.get('data', []):
            market = _market(position, markets, today)
            returns = {period: _return(market['points'], period, today)
                       if market['status'] in {'ready', 'stale'} else None
                       for period in RETURN_RANGES}
            value = rates.convert(number(position.get('value')), currency(position.get('currency')), currency_code, today_text)
            pnl = rates.convert(number(position.get('unrealized_pnl')), currency(position.get('currency')), currency_code, today_text)
            cost, native_price = number(position.get('average_cost')), number(position.get('price'))
            quantity = number(position.get('quantity'))
            day_move = None
            split_after_observation = _split_since_holdings(market, holdings_as_of, today)
            if split_after_observation and account['id'] in requested:
                split_affected.add(account['id'])
            if (market['status'] == 'ready' and not stale and quantity is not None
                    and returns['1d'] is not None and not split_after_observation):
                points = market['points']
                day_move = rates.convert(quantity * (points[-1]['close'] - points[-2]['close']),
                                         market['currency'], currency_code, points[-1]['date'])
            if day_move is not None:
                day_move_reason = None
            elif market['status'] == 'unsupported':
                day_move_reason = 'unsupported_security'
            elif market['status'] in {'unmatched', 'no_history'}:
                day_move_reason = 'missing_price_history'
            elif market['status'] == 'stale':
                day_move_reason = 'stale_prices'
            elif stale:
                day_move_reason = 'stale_holdings'
            elif quantity is None:
                day_move_reason = 'missing_quantity'
            elif split_after_observation:
                day_move_reason = 'share_units_changed'
            elif returns['1d'] is None:
                day_move_reason = 'missing_previous_session_close'
            else:
                day_move_reason = 'missing_exchange_rate'
            row = {
                'id': f"{account['id']}/{position.get('id')}", 'account_id': account['id'],
                'account_name': account.get('name'), 'institution': account.get('institution'),
                'position_id': position.get('id'), 'instrument_id': position.get('instrument_id'),
                'symbol': position.get('symbol'), 'name': position.get('name'),
                'asset_type': position.get('asset_type'), 'currency': position.get('currency'),
                'quantity': quantity, 'native_price': native_price, 'native_value': number(position.get('value')),
                'native_average_cost': cost, 'native_unrealized_pnl': number(position.get('unrealized_pnl')),
                'value': value, 'unrealized_pnl': pnl,
                # For a short, rising prices reduce the unrealized return.
                'unrealized_pct': (_percent(native_price, cost) * (-1 if quantity < 0 else 1))
                    if quantity is not None and quantity != 0 and cost is not None and cost > 0 and native_price is not None else None,
                'weight_pct': None, 'sector': market['sector'], 'market_symbol': market.get('symbol'),
                'market_currency': market['currency'], 'market_as_of': market['as_of'],
                'market_status': market['status'], 'returns': returns,
                'period_return_pct': _return(market['points'], range_name, today)
                    if market['status'] in {'ready', 'stale'} else None,
                'day_move_estimate': day_move, 'day_move_unavailable_reason': day_move_reason, 'is_cash': position.get('is_cash') is True, 'stale': stale,
            }
            rows.append(row)
            row['day_move_missing_date'] = (missing_previous_close(market['points'])
                if day_move_reason == 'missing_previous_session_close' else None)
        card = {key: account.get(key) for key in
                ('id', 'name', 'institution', 'number_suffix', 'type', 'connection_id', 'is_paper', 'currency')}
        card.update(native_value=number(account.get('total_value')),
                    value=rates.convert(number(account.get('total_value')), currency(account.get('currency')), currency_code, today_text),
                    cash=_cash(account, rates, currency_code, today_text),
                    holdings_count=sum(not r['is_cash'] for r in rows),
                    unrealized_pnl=_sum(r['unrealized_pnl'] for r in rows if not r['is_cash']),
                    day_move_estimate=_sum(r['day_move_estimate'] for r in rows),
                    holdings_as_of=holdings_as_of, using_saved_holdings=saved_positions,
                    balance_as_of=account.get('total_value_as_of'), stale=stale,
                    positions_status=positions.get('status', 'empty'), selected=account['id'] in requested,
                    positions_truncated=positions.get('truncated') is True,
                    priced_holdings=sum(r['day_move_estimate'] is not None for r in rows),
                    balances_status=account.get('balances', {}).get('status', 'empty'))
        accounts.append(card)
        if card['selected']:
            holdings.extend(rows)
    chosen = [a for a in accounts if a['selected']]
    stock_rows = [r for r in holdings if not r['is_cash']]
    known_accounts = sum(a['value'] is not None for a in chosen)
    total = _sum(a['value'] for a in chosen)
    weights_known = bool(chosen) and known_accounts == len(chosen) and total is not None and total > 0
    for row in holdings:
        row['weight_pct'] = row['value'] / total * 100 if weights_known and row['value'] is not None else None
    sectors = defaultdict(list)
    for row in stock_rows:
        sectors[row['sector']].append(row)
    sector_rows = []
    for name, rows in sectors.items():
        value = _sum(r['value'] for r in rows)
        sector_rows.append({'name': name, 'value': value,
            'weight_pct': value / total * 100 if weights_known and value is not None else None,
            'holdings_count': len(rows), 'priced_holdings': sum(r['day_move_estimate'] is not None for r in rows),
            'day_move_estimate': _sum(r['day_move_estimate'] for r in rows),
            'missing_holdings': sum(r['value'] is None for r in rows)})
    sector_rows.sort(key=lambda row: (row['value'] is None, -(row['value'] or 0), row['name']))
    summary = {'value': total, 'cash': _sum(a['cash'] for a in chosen),
        'unrealized_pnl': _sum(r['unrealized_pnl'] for r in stock_rows),
        'day_move_estimate': _sum(r['day_move_estimate'] for r in stock_rows),
        'holdings_count': len(stock_rows), 'selected_accounts': len(chosen),
        'known_accounts': known_accounts, 'missing_accounts': len(chosen) - known_accounts,
        'valued_holdings': sum(r['value'] is not None for r in stock_rows),
        'priced_holdings': sum(r['day_move_estimate'] is not None for r in stock_rows),
        'classified_holdings': sum(r['sector'] not in {'Unclassified', 'Funds & ETFs'} for r in stock_rows),
        'direct_holdings': sum(r['sector'] != 'Funds & ETFs' for r in stock_rows),
        'fund_holdings': sum(r['sector'] == 'Funds & ETFs' for r in stock_rows),
        'unclassified_holdings': sum(r['sector'] == 'Unclassified' for r in stock_rows),
        'pnl_holdings': sum(r['unrealized_pnl'] is not None for r in stock_rows),
        'partial': bool(known_accounts != len(chosen) or any(a['stale'] or a['positions_truncated'] for a in chosen)
                        or any(r['value'] is None or r['unrealized_pnl'] is None for r in stock_rows))}
    warnings = []
    if not raw_accounts:
        warnings.append('No connected portfolio is cached yet. Refresh the connection to save a portfolio snapshot.')
    if paper:
        warnings.append('Paper accounts contain simulated money and are shown separately from live accounts.')
    if summary['missing_accounts']:
        warnings.append('Some account totals or exchange rates are unavailable. The displayed value includes only known accounts.')
    if any(a['stale'] for a in chosen):
        warnings.append('Some holdings are stale. Their account values remain visible, but daily move estimates are withheld.')
    if any(a['using_saved_holdings'] and a['priced_holdings'] for a in chosen):
        warnings.append('Some session estimates use a recent, complete saved holdings snapshot because the latest holdings refresh failed. The original holdings time is preserved.')
    if any((a['positions_status'] not in {'ok', 'empty'} and not a['using_saved_holdings'])
           or a['positions_truncated'] for a in chosen):
        warnings.append('Some accounts have incomplete holdings coverage. Account totals and individual holding totals may differ.')
    if summary['priced_holdings'] < len(stock_rows):
        warnings.append('Daily estimates cover only holdings with verified recent price history, current quantities and available exchange rates.')
    if split_affected:
        warnings.append('A stock split occurred on or after a saved holdings date. Affected session money estimates are withheld until a later holdings observation establishes the share units; security price returns remain available.')
    if any(r['market_status'] == 'stale' for r in stock_rows):
        warnings.append('Some security price returns use older market closes. Check the market date on each holding.')
    return {
        'retrieved_at': cached.get('retrieved_at'),
        'balance_as_of': _earliest_timestamp(a['balance_as_of'] for a in chosen if a['value'] is not None),
        'holdings_as_of': _earliest_timestamp(a['holdings_as_of'] for a in chosen if a['holdings_count']),
        'market_as_of': max((r['market_as_of'] for r in stock_rows if r['market_as_of']), default=None),
        'currency': currency_code, 'range': range_name, 'selected_account_ids': requested, 'paper': paper,
        'accounts': accounts, 'summary': summary, 'holdings': holdings, 'sectors': sector_rows,
        'warnings': warnings, 'methodology': [
            'Account value comes from the connected broker and already includes cash. Cash is not added twice.',
            'Security returns compare market closes in the listing currency. They exclude dividends, fees and FX, and are not your personal investment return.',
            'Daily move estimates apply the latest session price change versus the prior close to currently cached shares, with dated FX where needed. The latest session may still be open. These estimates do not reconstruct intraday trades or actual daily account profit.',
            'A latest-session return requires a close pair spanning one weekday. Missing-session and unverified holiday gaps are withheld, and split events on or after the holdings date prevent a monetary estimate from incompatible share units.',
            'Unrealized gain uses the broker cost basis of currently held positions. Realized gains and dividends are separate.',
            'Weights use selected account totals, including cash. Sector values cover direct holdings; funds are grouped separately without a look-through claim.',
            'Live and paper accounts are never combined. Positions remain separate by account and provider instrument identity.',
        ]}


def security(conn, *, account_id, position_id, path=None, currency_code='GBP', range_name='1m',
             today=None, markets=None, cached=None):
    today = today or datetime.now(timezone.utc).date()
    cached = snaptrade_store.load_portfolio(path=path) if cached is None else cached
    if markets is None:
        from .market_cache import load_market_data
        instruments = {position['instrument_id'] for account in (cached or {}).get('accounts', [])
                       if account['id'] == account_id for position in account.get('positions', {}).get('data', [])
                       if position.get('instrument_id')}
        # Keep selected-account coverage warnings intact without decoding the
        # other accounts' histories. Identity conflicts still inspect every
        # current account below, even when only one listing's chart is opened.
        markets = load_market_data(path, instrument_ids=instruments)
    markets = _consistent_markets(cached or {}, markets)
    result = bundle(conn, path=path, currency_code=currency_code, range_name=range_name,
                    account_ids=[account_id], today=today, markets=markets, cached=cached)
    holding = next((r for r in result['holdings'] if r['position_id'] == position_id), None)
    if holding is None:
        raise LookupError('This holding was not found in the selected account.')
    source = next(p for a in (cached or {}).get('accounts', []) if a['id'] == account_id
                  for p in a.get('positions', {}).get('data', []) if p.get('id') == position_id)
    market = _market(source, markets, today)
    points = market['points'] if market['status'] in {'ready', 'stale'} else []
    cutoff = _cutoff(range_name, today)
    if cutoff:
        anchor = _anchor(points, cutoff)
        points = ([anchor] if anchor else []) + [p for p in points if p['date'] > cutoff.isoformat()]
    first = points[0]['close'] if points else None
    points = [{**p, 'return_pct': _percent(p['close'], first)} for p in points]
    return {'holding': holding, 'currency': currency_code, 'range': range_name, 'points': points,
            'market_currency': market['currency'], 'price_basis': 'price_return',
            'source_url': market.get('source_url'), 'warnings': result['warnings'],
            'methodology': result['methodology']}

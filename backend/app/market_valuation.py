"""Hourly market movement of a fixed, current portfolio basket.

This is deliberately separate from account history: current quantities and cash
are repriced at earlier completed market bars. It cannot establish past trades,
cash flows or personal investment returns. All reads are local.
"""
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
import hashlib

from . import snaptrade_store, wealth
from .market_cache import listing
from .snaptrade_models import currency, number

RANGES = {'1d': 1, '1w': 7, '1m': 30}
MAX_QUOTE_AGE = timedelta(days=4)


def timestamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def iso(value):
    return value.isoformat().replace('+00:00', 'Z') if value else None


def bundle(conn, *, path=None, currency_code='GBP', range_name='1d', interval='1h',
           account_ids=None, now=None, cached=None, markets=None):
    now = now or datetime.now(timezone.utc)
    if range_name not in RANGES or interval not in {'1h', '1d'} or not currency(currency_code):
        raise ValueError('Choose a supported recent-price range, interval and currency.')
    cached = snaptrade_store.load_portfolio(path=path) if cached is None else cached
    cached = cached or {'accounts': []}
    accounts = cached.get('accounts', [])
    requested = snaptrade_store.selected_account_ids(accounts, account_ids)
    selected = [account for account in accounts if account['id'] in requested]
    if len({bool(account.get('is_paper')) for account in selected}) > 1:
        raise ValueError('View live and paper accounts separately.')
    if markets is None:
        from .intraday_market import read_intraday
        markets = read_intraday(conn)
    rates = wealth.Rates(conn)
    supported, warnings, positions, source_dates = [], [], [], []
    for account in selected:
        holdings, balances = account.get('positions', {}), account.get('balances', {})
        observed = timestamp(account.get('holdings_synced_at'))
        usable = (holdings.get('status') in {'ok', 'empty'} and balances.get('status') in {'ok', 'empty'}
                  and not holdings.get('truncated') and not balances.get('truncated')
                  and account.get('initial_sync_complete') is not False
                  and observed is not None and observed <= now)
        rows, cash = [], []
        seen = set()
        for position in holdings.get('data', []):
            quantity = number(position.get('quantity'))
            if quantity == 0:
                continue
            expected = listing(position)
            if (not expected or quantity is None or position.get('is_cash')
                    or position.get('multiplier') not in (None, 1, 1.0)
                    or not position.get('id') or position['id'] in seen):
                usable = False
                continue
            seen.add(position['id'])
            market = markets.get(expected['instrument_id'], {})
            if observed and any(observed < event_time <= now
                                for event in market.get('splits', [])
                                if (event_time := timestamp(event.get('timestamp'))) is not None):
                usable = False
                warnings.append(f"{account['name']}: a split occurred after the saved share quantities. Refresh brokerage holdings before valuing this account with adjusted prices.")
            rows.append({'quantity': quantity, 'expected': expected})
        cash_currencies = set()
        for balance in balances.get('data', []):
            amount, code = number(balance.get('cash')), currency(balance.get('currency'))
            if amount is None or code is None or code in cash_currencies:
                usable = False
            else:
                cash_currencies.add(code)
                cash.append((amount, code))
        if not rows and not any(amount for amount, _ in cash) and number(account.get('total_value')) not in (0, 0.0):
            usable = False  # An absent breakdown is not proof of an empty account.
        if not rows and observed and number(account.get('total_value')) is not None:
            # A cash-only breakdown must explain the same saved broker total;
            # otherwise missing positions could be presented as a tiny portfolio.
            broker_currency = currency(account.get('currency'))
            cash_values = [(0 if amount == 0 else rates.convert(amount, code, broker_currency, observed.date().isoformat()))
                           for amount, code in cash] if broker_currency else [None]
            broker_value = number(account['total_value'])
            if any(value is None for value in cash_values) or abs(sum(cash_values) - broker_value) > max(1, abs(broker_value) * .01):
                usable = False
        if not usable:
            warnings.append(f"{account['name']}: a complete dated holdings and cash breakdown is unavailable; this account is excluded from the market estimate.")
            continue
        supported.append({'id': account['id'], 'positions': rows, 'cash': cash})
        positions.extend(rows)
        source_dates.append(observed)
        if (holdings.get('stale') or balances.get('stale') or now - observed > timedelta(days=2)):
            warnings.append(f"{account['name']}: quantities or cash are from an older saved brokerage observation.")

    # Identical symbols on different venues are not interchangeable. Conflicting
    # identities sharing one provider ID invalidate that series.
    expected_by_id, conflicts = {}, set()
    for row in positions:
        expected = row['expected']; identifier = expected['instrument_id']
        if identifier in expected_by_id and expected_by_id[identifier] != expected:
            conflicts.add(identifier)
        expected_by_id[identifier] = expected
    histories = {}
    for identifier, expected in expected_by_id.items():
        market = markets.get(identifier, {})
        if (identifier in conflicts or market.get('symbol') != expected['symbol']
                or market.get('currency') != expected['currency'] or market.get('exchange') not in expected['exchanges']):
            continue
        points = sorted((timestamp(point.get('timestamp')), number(point.get('close')))
                        for point in market.get('points', [])
                        if timestamp(point.get('timestamp')) is not None and number(point.get('close')) is not None
                        and number(point.get('close')) > 0 and timestamp(point['timestamp']) <= now)
        if points:
            histories[identifier] = (points, [point[0] for point in points])
        if market.get('error') or market.get('status') == 'stale':
            warnings.append('Some hourly prices are retained from an earlier refresh. Their original observation times are shown.')

    start = now - timedelta(days=RANGES[range_name])
    # Include exact range boundaries as well as UTC hours. US bars commonly end
    # at :30; the current endpoint must not hold their completed price until :00.
    grid = [start]
    next_hour = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    while next_hour < now:
        grid.append(next_hour)
        next_hour += timedelta(hours=1)
    grid.append(now)
    points, used_times = [], []
    session_by_time = {identifier: {timestamp(point['timestamp']): point.get('session', 'regular') for point in market.get('points', [])} for identifier, market in markets.items()}
    coverage = hashlib.sha256('|'.join(sorted(account['id'] for account in supported)).encode()).hexdigest()[:16]
    for when in grid:
        total, priced, quote_times, complete_accounts = 0., 0, [], 0
        used_sessions = set()
        for account in supported:
            value, complete = 0., True
            for row in account['positions']:
                expected = row['expected']; history, times = histories.get(expected['instrument_id'], ([], []))
                index = bisect_right(times, when) - 1
                close = history[index] if index >= 0 else None
                if close is None or when - close[0] > MAX_QUOTE_AGE:
                    complete = False
                    continue
                converted = rates.convert(row['quantity'] * close[1], expected['currency'], currency_code, when.date().isoformat())
                if converted is None:
                    complete = False
                    continue
                value += converted; priced += 1; quote_times.append(close[0])
                used_sessions.add(session_by_time.get(expected['instrument_id'], {}).get(close[0], 'regular'))
            for amount, code in account['cash']:
                if amount == 0:
                    continue
                converted = rates.convert(amount, code, currency_code, when.date().isoformat())
                if converted is None:
                    complete = False
                else:
                    value += converted
            if complete:
                total += value; complete_accounts += 1
        complete = bool(supported) and complete_accounts == len(supported)
        if complete:
            used_times.extend(quote_times)
        points.append({'timestamp': iso(when), 'value': round(total, 2) if complete else None,
            'priced_holdings': priced, 'holdings_count': len(positions), 'known_accounts': complete_accounts,
            'missing_accounts': len(selected) - complete_accounts, 'oldest_price_at': iso(min(quote_times)) if quote_times else None,
            'newest_price_at': iso(max(quote_times)) if quote_times else None,
            'sessions': sorted(used_sessions), 'estimated': True, 'coverage_key': coverage})
    trimmed = False
    while points and points[0]['value'] is None:
        points.pop(0)
        trimmed = True
    if trimmed and points:
        warnings.append('The available public price window starts after the requested range boundary. Movement uses the first supported estimate; the observed dates are shown below the chart.')
    known = [point for point in points if point['value'] is not None]
    latest = points[-1] if points else None
    comparable = len(points) >= 2 and all(point['value'] is not None for point in points)
    change = round(points[-1]['value'] - points[0]['value'], 2) if comparable else None
    partial = bool(len(supported) != len(selected) or warnings or not latest or len(known) != len(points))
    if any(point['value'] is None for point in points):
        warnings.append('Some hours lack required prices or daily reference FX. Those estimates remain gaps.')
    if supported and not known:
        warnings.append('Hourly prices have not yet covered every required holding and cash currency. Refresh market prices to populate the local cache.')
    # Sampling changes the chart density, never the comparison window or its
    # completeness. Preserve the exact first estimate and gap boundaries so
    # daily sampling cannot change the headline return or conceal missing hours.
    change_pct = change / abs(points[0]['value']) * 100 if change is not None and points[0]['value'] else None
    if interval == '1d' and points:
        daily = {point['timestamp'][:10]: index for index, point in enumerate(points)}
        keep = {0, *daily.values()}
        for index in range(1, len(points)):
            if (points[index]['value'] is None) != (points[index - 1]['value'] is None):
                keep.update((index - 1, index))
        points = [points[index] for index in sorted(keep)]
    return {'generated_at': iso(now), 'currency': currency_code, 'range': range_name, 'interval': interval,
        'basis': 'current_holdings', 'holdings_as_of': iso(min(source_dates)) if source_dates else None,
        'market_as_of': iso(max(used_times)) if used_times else None, 'selected_account_ids': requested,
        'status': 'unavailable' if not known else 'partial' if partial else 'ready',
        'summary': {'value': latest['value'] if latest else None, 'change': change,
            'change_pct': change_pct,
            'selected_accounts': len(selected), 'known_accounts': latest['known_accounts'] if latest else 0,
            'positions': len(positions), 'priced_positions': latest['priced_holdings'] if latest else 0, 'partial': partial},
        'points': points, 'warnings': list(dict.fromkeys(warnings)), 'methodology': [
            'Current saved quantities and cash are held fixed across the selected period. This is a hypothetical market-value path, not past account value or personal return.',
            'Prices are verified completed hourly bars, including pre-market and after-hours where supplied. Intraday prices may be delayed. A close becomes usable after its bar ends, never at its opening timestamp.',
            'The latest completed price is carried through closed-market hours for at most four days. Flat hours are carried marks, not new exchange quotes; each point includes its oldest constituent price time.',
            'Daily chart sampling keeps the comparison start, each UTC day\'s last estimate and gap boundaries. Sampling does not change period movement or hide missing hourly coverage.',
            'Daily reference FX is held constant within each UTC day. This view does not contain intraday FX ticks, past trades, cash flows or dividend reinvestment.',
            'Broker-reported current account totals remain unchanged. This separate market estimate may differ because of quote times, reference FX and unavailable holdings.',
        ]}

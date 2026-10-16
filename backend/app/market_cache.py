"""Replaceable public-market adapter and normalized local security history.

Only public listing symbols go upstream. Broker identities, positions, accounts,
balances and credentials never do. Raw provider payloads are not persisted.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import json
import re
import threading
import time
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db, prices, snaptrade_store
from .snaptrade_models import number, text

SCHEMA = '''
CREATE TABLE IF NOT EXISTS investment_market_cache (
    instrument_id TEXT PRIMARY KEY,
    metadata TEXT NOT NULL,
    fetched_at TEXT,
    attempted_at TEXT NOT NULL,
    refresh_error TEXT
);
CREATE TABLE IF NOT EXISTS investment_market_closes (
    instrument_id TEXT NOT NULL,
    date TEXT NOT NULL,
    close REAL NOT NULL CHECK(close > 0),
    PRIMARY KEY(instrument_id,date)
);
'''

# SnapTrade MIC/code -> Yahoo suffix, accepted venue codes, listing currency.
# No inferred US listing merely because a ticker happens to exist there.
VENUES = {
    'XNYS': ('', {'NYQ'}, 'USD'), 'NYSE': ('', {'NYQ'}, 'USD'),
    'XNAS': ('', {'NMS', 'NGM', 'NCM', 'NAS'}, 'USD'),
    'NASDAQ': ('', {'NMS', 'NGM', 'NCM', 'NAS'}, 'USD'),
    'ARCX': ('', {'PCX', 'ASE'}, 'USD'), 'ARCA': ('', {'PCX', 'ASE'}, 'USD'),
    'XASE': ('', {'ASE'}, 'USD'), 'BATS': ('', {'BTS'}, 'USD'), 'BZX': ('', {'BTS'}, 'USD'),
    'XLON': ('.L', {'LSE'}, None), 'LSE': ('.L', {'LSE'}, None),
    'XSES': ('.SI', {'SES'}, 'SGD'), 'SGX': ('.SI', {'SES'}, 'SGD'),
    'XHKG': ('.HK', {'HKG'}, 'HKD'), 'HKEX': ('.HK', {'HKG'}, 'HKD'),
    'XTSE': ('.TO', {'TOR'}, 'CAD'), 'TSX': ('.TO', {'TOR'}, 'CAD'),
    'XASX': ('.AX', {'ASX'}, 'AUD'), 'ASX': ('.AX', {'ASX'}, 'AUD'),
    'XPAR': ('.PA', {'PAR'}, 'EUR'), 'XAMS': ('.AS', {'AMS'}, 'EUR'),
    'XETR': ('.DE', {'GER'}, 'EUR'),
}
SUPPORTED = {'stock', 'adr', 'etf', 'cef'}
BENCHMARKS = [
    {'instrument_id': 'index:sp500', 'name': 'S&P 500', 'symbol': '^GSPC', 'currency': 'USD', 'exchanges': {'SNP'}, 'asset_type': 'index'},
    {'instrument_id': 'index:nasdaq', 'name': 'Nasdaq Composite', 'symbol': '^IXIC', 'currency': 'USD', 'exchanges': {'NIM'}, 'asset_type': 'index'},
    {'instrument_id': 'index:ftse100', 'name': 'FTSE 100', 'symbol': '^FTSE', 'currency': 'GBP', 'exchanges': {'FGI'}, 'asset_type': 'index'},
    {'instrument_id': 'index:hangseng', 'name': 'Hang Seng', 'symbol': '^HSI', 'currency': 'HKD', 'exchanges': {'HKG'}, 'asset_type': 'index'},
    {'instrument_id': 'index:sti', 'name': 'Straits Times', 'symbol': '^STI', 'currency': 'SGD', 'exchanges': {'SES'}, 'asset_type': 'index'},
]
MAX_SECURITIES = 120
REFRESH_SECONDS = 60
QUOTE_TTL_SECONDS = 300
_lock = threading.Lock()
_next_refresh = 0.0


class RefreshBusy(Exception):
    pass


def database(path=None):
    connection = db.connect(path or db.DB_PATH)
    try:
        connection.executescript(SCHEMA)
    except BaseException:
        connection.close()
        raise
    return connection


def listing(position):
    """Require a provider instrument ID, explicit venue and listing currency."""
    if position.get('asset_type') not in SUPPORTED or position.get('is_cash'):
        return None
    identifier = position.get('instrument_id')
    raw = position.get('raw_symbol')
    venue = VENUES.get((position.get('exchange') or '').upper())
    currency = position.get('listing_currency')
    if not identifier or not isinstance(raw, str) or not re.fullmatch(r'[A-Za-z0-9.\-]{1,20}', raw) or not venue or not currency:
        return None
    suffix, exchanges, expected = venue
    if expected and currency != expected:
        return None
    symbol = raw.upper()
    if suffix == '.HK' and symbol.isdigit():
        symbol = str(int(symbol)).zfill(4)
    if not suffix:
        symbol = symbol.replace('.', '-')
    symbol += suffix
    return {'instrument_id': identifier, 'symbol': symbol, 'currency': currency,
            'exchanges': exchanges, 'asset_type': position['asset_type']}


def normalize_chart(body, expected, *, fetched_at=None):
    """Yahoo Close is split-adjusted; deliberately do not use Adj Close.

    Adj Close includes distributions, which cannot be mixed with separately
    accounted dividends. A session's last point can still be in progress.
    """
    result = body['chart']['result'][0]
    if body['chart'].get('error'):
        raise ValueError('Market data unavailable')
    meta = result['meta']
    unit = prices._currency(meta.get('currency'))
    code = prices._major_currency(unit)
    kind = meta.get('instrumentType')
    valid_type = kind in {'etf': {'ETF'}, 'stock': {'EQUITY'}, 'adr': {'EQUITY'}, 'cef': {'EQUITY', 'ETF'}, 'index': {'INDEX'}}[expected['asset_type']]
    if (meta.get('symbol') != expected['symbol'] or meta.get('exchangeName') not in expected['exchanges']
            or code != expected['currency'] or not valid_type):
        raise ValueError('Listing metadata did not match')
    epoch = number(meta.get('regularMarketTime'))
    if epoch is None or epoch <= 0:
        raise ValueError('Market timestamp missing')
    as_of = datetime.fromtimestamp(epoch, timezone.utc)
    if as_of.timestamp() > time.time() + 300:
        raise ValueError('Future market data')
    try:
        zone = ZoneInfo(meta['exchangeTimezoneName'])
    except (KeyError, TypeError, ZoneInfoNotFoundError):
        raise ValueError('Exchange timezone missing') from None
    market_day = as_of.astimezone(zone).date()
    timestamps = result.get('timestamp', [])
    closes = result['indicators']['quote'][0]['close']
    if len(timestamps) != len(closes):
        raise ValueError('Price history incomplete')
    normalized = {}
    multiplier = .01 if unit == 'GBp' else 1
    for stamp, raw in zip(timestamps, closes):
        value, ts = number(raw), number(stamp)
        if value is None or value <= 0 or ts is None or ts > epoch + 86400:
            continue
        when = datetime.fromtimestamp(ts, zone).date()
        if when > market_day:
            continue
        key = when.isoformat()
        if key in normalized and normalized[key] != value * multiplier:
            raise ValueError('Conflicting daily prices')
        normalized[key] = value * multiplier
    if not normalized:
        raise ValueError('No valid closing prices')
    # Close is adjusted for splits after the historic session. Keep the event
    # factors so statement quantities can be valued in their original units.
    # Missing metadata in an older cache is deliberately not equivalent to no
    # splits; those caches need one explicit refresh before reconstruction.
    events = result.get('events') or {}
    raw_splits = events.get('splits') or {}
    if not isinstance(raw_splits, dict):
        raise ValueError('Split history incomplete')
    splits = {}
    for item in raw_splits.values():
        stamp = number(item.get('date'))
        numerator, denominator = number(item.get('numerator')), number(item.get('denominator'))
        if stamp is None or numerator is None or denominator is None or min(numerator, denominator) <= 0:
            raise ValueError('Invalid split history')
        when = datetime.fromtimestamp(stamp, zone).date()
        if when > market_day:
            continue
        value = {'date': when.isoformat(), 'numerator': numerator, 'denominator': denominator}
        if value['date'] in splits and splits[value['date']] != value:
            raise ValueError('Conflicting split history')
        splits[value['date']] = value
    points = [{'date': day, 'close': normalized[day]} for day in sorted(normalized)][-600:]
    return {'instrument_id': expected['instrument_id'], 'symbol': expected['symbol'], 'currency': code,
            'exchange': meta.get('exchangeName'), 'sector': None, 'industry': None,
            'as_of': as_of.isoformat(), 'fetched_at': fetched_at or datetime.now(timezone.utc).isoformat(),
            'status': 'ok', 'error': None, 'source_url': 'https://finance.yahoo.com/quote/' + quote(expected['symbol'], safe='') + '/history/',
            'points': points, 'split_adjusted': True, 'dividends_included': False,
            'splits': [splits[when] for when in sorted(splits)], 'split_events_complete': True}


def missing_previous_close(points):
    """A missing weekday is a coverage gap, not evidence of a zero daily move.

    A venue holiday may explain the gap; no synthetic holiday close is added.
    """
    if not points:
        return None
    previous = date.fromisoformat(points[-1]['date']) - timedelta(days=1)
    while previous.weekday() >= 5:
        previous -= timedelta(days=1)
    return previous.isoformat() if not any(p['date'] == previous.isoformat() for p in points) else None


def fill_price_gaps(current, previous):
    """Retain real dated closes only on an independently verified identical basis.

    Refreshes can temporarily return nulls for previously available dates. An
    unchanged split basis lets us keep that evidence without redating it. Never
    carry prices forward, mix listing identities, or preserve an old split basis.
    """
    if (not current.get('points') or not previous.get('points')
            or not all(current.get(k) == previous.get(k) for k in ('instrument_id', 'symbol', 'currency', 'exchange'))
            or current.get('split_events_complete') is not True or previous.get('split_events_complete') is not True
            or current.get('split_adjusted') is not True or previous.get('split_adjusted') is not True
            or current.get('dividends_included') is not False or previous.get('dividends_included') is not False):
        return current
    points = {p['date']: p for p in current['points']}
    start, end = min(points), max(points)
    missing = [p for p in previous['points'] if start <= p['date'] <= end and p['date'] not in points]
    if not missing:
        return current
    first = min(p['date'] for p in missing)
    # Only events after a retained close can change its adjustment basis.
    split_basis = lambda data: sorted((s['date'], s['numerator'], s['denominator'])
                                     for s in data.get('splits', []) if s['date'] >= first)
    if split_basis(current) != split_basis(previous):
        return current
    old_receipts = {p['date']: p['fetched_at'] for p in previous.get('retained_closes', [])}
    receipts = {p['date']: p for p in current.get('retained_closes', [])}
    for point in missing:
        points[point['date']] = point
        receipts[point['date']] = {'date': point['date'],
            'fetched_at': old_receipts.get(point['date'], previous.get('fetched_at'))}
    return {**current, 'points': [points[day] for day in sorted(points)],
            'retained_closes': [receipts[day] for day in sorted(receipts)]}


def fetch_listing(expected, read=prices._get_json):
    symbol = expected['symbol']
    body = read('https://query1.finance.yahoo.com/v8/finance/chart/' + quote(symbol, safe='') +
                '?range=2y&interval=1d&events=splits&includePrePost=false')
    value = normalize_chart(body, expected)
    if missing_previous_close(value['points']):
        # One bounded retry of the recent window can recover a missing daily
        # row in a long-range response. Every returned listing is revalidated.
        try:
            recent = read('https://query1.finance.yahoo.com/v8/finance/chart/' + quote(symbol, safe='') +
                          '?range=5d&interval=1d&events=splits&includePrePost=false')
            value = fill_price_gaps(value, normalize_chart(recent, expected))
        except Exception:
            pass  # Keep the valid primary history and expose its coverage gap.
    if expected['asset_type'] == 'index':
        return value
    # Sector metadata is accepted only for the exact same verified listing.
    try:
        search = read('https://query1.finance.yahoo.com/v1/finance/search?' + urlencode({
            'q': symbol, 'quotesCount': 6, 'newsCount': 0, 'enableFuzzyQuery': 'false'}))
    except Exception:
        search = {}
    quotes = search.get('quotes') if isinstance(search, dict) else None
    candidates = [r for r in (quotes if isinstance(quotes, list) else []) if isinstance(r, dict)
        and r.get('symbol') == symbol and r.get('exchange') in expected['exchanges']]
    if len(candidates) == 1 and expected['asset_type'] in {'stock', 'adr'}:
        value['sector'] = text(candidates[0].get('sectorDisp') or candidates[0].get('sector'), limit=100)
        value['industry'] = text(candidates[0].get('industryDisp') or candidates[0].get('industry'), limit=180)
    return value


def load_market_data(path=None, *, instrument_ids=None):
    conn = database(path)
    try:
        return read_market_data(conn, instrument_ids=instrument_ids, schema_known=True)
    finally:
        conn.close()


def read_market_data(conn, *, instrument_ids=None, schema_known=False):
    """Read a consistent caller-owned snapshot, without migrations or writes."""
    owns_snapshot = not conn.in_transaction
    if owns_snapshot:
        conn.execute('BEGIN')
    try:
        if not schema_known:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'investment_market_cache', 'investment_market_closes'} <= tables:
                return {}
        ids = tuple(dict.fromkeys(instrument_ids)) if instrument_ids is not None else None
        if ids == ():
            return {}
        clause = ' WHERE instrument_id IN (' + ','.join('?' for _ in ids) + ')' if ids is not None else ''
        args, result = ids or (), {}
        for row in conn.execute('SELECT * FROM investment_market_cache' + clause, args):
            value = json.loads(row['metadata'])
            value.update(fetched_at=row['fetched_at'], error=row['refresh_error'], attempted_at=row['attempted_at'], points=[])
            result[row['instrument_id']] = value
        for row in conn.execute('SELECT instrument_id,date,close FROM investment_market_closes' + clause + ' ORDER BY instrument_id,date', args):
            if row['instrument_id'] in result:
                result[row['instrument_id']]['points'].append({'date': row['date'], 'close': row['close']})
        return result
    finally:
        if owns_snapshot:
            conn.rollback()


def _retain_classification(metadata, previous, expected, fetched_at):
    """Optional profile omissions do not erase an exact verified listing's facts.

    Classification has its own observation clock; refreshing prices does not
    make an older retained sector or industry newly observed. A changed listing
    or asset kind cannot inherit the previous instrument's classification.
    """
    same_listing = (
        previous.get('status') == metadata.get('status') == 'ok'
        and expected['asset_type'] in {'stock', 'adr'}
        and previous.get('asset_type', expected['asset_type']) == expected['asset_type']
        and previous.get('instrument_id') == metadata.get('instrument_id') == expected['instrument_id']
        and previous.get('symbol') == metadata.get('symbol') == expected['symbol']
        and previous.get('currency') == metadata.get('currency') == expected['currency']
        and previous.get('exchange') == metadata.get('exchange')
        and metadata.get('exchange') in expected['exchanges']
    )
    metadata['asset_type'] = expected['asset_type']
    for field, limit in (('sector', 100), ('industry', 180)):
        current = text(metadata.get(field), limit=limit)
        old = text(previous.get(field), limit=limit) if same_listing else None
        metadata[field] = current or old
        if current:
            metadata[field + '_as_of'] = fetched_at
        elif old:
            metadata[field + '_as_of'] = previous.get(field + '_as_of') or previous.get('fetched_at')
        else:
            metadata[field + '_as_of'] = None
    return metadata


def refresh_market_data(path=None, *, portfolio=None, fetch=fetch_listing, enforce_cooldown=True, include_benchmarks=False):
    global _next_refresh
    if not _lock.acquire(blocking=False):
        raise RefreshBusy('Market data is already refreshing.')
    try:
        if enforce_cooldown and time.monotonic() < _next_refresh:
            raise RefreshBusy('Wait a minute before refreshing market data again.')
        _next_refresh = time.monotonic() + REFRESH_SECONDS
        cached = load_market_data(path)
        portfolio = portfolio if portfolio is not None else snaptrade_store.load_portfolio(path)
        instruments, unsupported, conflicts = {}, set(), set()
        for account in (portfolio or {}).get('accounts', []):
            for position in account.get('positions', {}).get('data', []):
                expected = listing(position)
                if expected:
                    identifier = expected['instrument_id']
                    if identifier in instruments and instruments[identifier] != expected:
                        conflicts.add(identifier)
                    instruments[identifier] = expected
                else:
                    unsupported.add(position.get('instrument_id') or position.get('id'))
        for identifier in conflicts:
            instruments.pop(identifier, None)
            unsupported.add(identifier)
        # Keep verified closed holdings in the historical price cache. Their
        # public listing identity is reviewed independently of account links.
        from .historical_listings import LISTINGS
        conn = database(path)
        try:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='transactions' AND type='table'").fetchone():
                for item in LISTINGS:
                    present = conn.execute("SELECT 1 FROM transactions WHERE CASE WHEN json_valid(meta) THEN json_extract(meta,'$.isin') END=? LIMIT 1", (item['isin'],)).fetchone()
                    if present:
                        expected = listing(item)
                        if expected:
                            instruments[expected['instrument_id']] = expected
        finally:
            conn.close()
        if include_benchmarks:
            instruments.update({item['instrument_id']: item for item in BENCHMARKS})
        now = datetime.now(timezone.utc)
        wanted = []
        for id, expected in instruments.items():
            previous = cached.get(id, {})
            try:
                identity_matches = (previous.get('symbol') == expected['symbol']
                    and previous.get('currency') == expected['currency']
                    and previous.get('exchange') in expected['exchanges'])
                fresh = identity_matches and previous.get('split_events_complete') is True and (now - datetime.fromisoformat(previous['fetched_at'])).total_seconds() < QUOTE_TTL_SECONDS
            except (KeyError, TypeError, ValueError):
                fresh = False
            if not fresh:
                wanted.append(expected)
        omitted = max(0, len(wanted) - MAX_SECURITIES)
        wanted = wanted[:MAX_SECURITIES]
        deadline = time.monotonic() + 70
        def run(expected):
            try:
                if time.monotonic() >= deadline:
                    return expected, None
                return expected, fetch(expected)
            except Exception:
                return expected, None
        attempted_at = now.isoformat()
        updated, failed, retained = 0, 0, 0
        # No SQLite write transaction is held while network requests run.
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(run, wanted))
        conn = database(path)
        try:
            for expected, result in results:
                id = expected['instrument_id']
                if result:
                    result = fill_price_gaps(result, cached.get(id, {}))
                    points = result['points']
                    meta = {key: result.get(key) for key in ('instrument_id','symbol','currency','exchange','sector','industry','as_of','status','source_url','split_adjusted','dividends_included','splits','split_events_complete')}
                    meta['retained_closes'] = result.get('retained_closes', [])
                    meta = _retain_classification(meta, cached.get(id, {}), expected, result['fetched_at'])
                    conn.execute('INSERT INTO investment_market_cache VALUES(?,?,?,?,NULL) ON CONFLICT(instrument_id) DO UPDATE SET metadata=excluded.metadata,fetched_at=excluded.fetched_at,attempted_at=excluded.attempted_at,refresh_error=NULL',
                                 (id, json.dumps(meta, allow_nan=False), result['fetched_at'], attempted_at))
                    # Replace the provider window atomically. Mixing older
                    # closes adjusted to a previous split basis is unsafe.
                    conn.execute('DELETE FROM investment_market_closes WHERE instrument_id=?', (id,))
                    conn.executemany('INSERT INTO investment_market_closes VALUES(?,?,?)', [(id, p['date'], p['close']) for p in points])
                    updated += 1
                else:
                    failed += 1
                    message = 'Public market data could not be verified or retrieved for this listing.'
                    if id in cached:
                        conn.execute('UPDATE investment_market_cache SET attempted_at=?,refresh_error=? WHERE instrument_id=?', (attempted_at, message, id))
                        retained += 1
                    else:
                        meta = {'instrument_id': id, 'symbol': expected['symbol'], 'currency': expected['currency'], 'status': 'unavailable', 'sector': None, 'industry': None, 'as_of': None, 'source_url': None}
                        conn.execute('INSERT INTO investment_market_cache VALUES(?,?,NULL,?,?)', (id, json.dumps(meta), attempted_at, message))
            conn.commit()
            saved = read_market_data(conn)
            incomplete = sum(bool(missing_previous_close(saved.get(identifier, {}).get('points', [])))
                             for identifier in instruments)
        finally:
            conn.close()
        return {'attempted': len(wanted), 'updated': updated, 'failed': failed, 'retained': retained,
                'incomplete': incomplete,
                'unsupported': len(unsupported), 'fetched_at': attempted_at,
                'message': ('Some market data is unavailable; previous verified observations are retained.' if failed else
                            f'Prices saved; {incomplete} listings still lack a previous weekday close. Session moves remain partial; market holidays can also cause gaps.' if incomplete else 'Market data updated.'),
                'omitted': omitted}
    finally:
        _lock.release()

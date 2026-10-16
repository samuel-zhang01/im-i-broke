"""Verified hourly public listing prices in a private, replaceable local cache.

Yahoo timestamps identify bar openings. Public ``timestamp`` below identifies
when a completed bar's close became available, never its opening. Incomplete
bars are omitted. Only public listing symbols leave this module.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db, market_cache, prices, snaptrade_store
from .snaptrade_models import number


SCHEMA = '''
CREATE TABLE IF NOT EXISTS investment_intraday_cache (
    instrument_id TEXT PRIMARY KEY,
    metadata TEXT NOT NULL,
    fetched_at TEXT,
    attempted_at TEXT NOT NULL,
    refresh_error TEXT
);
CREATE TABLE IF NOT EXISTS investment_intraday_bars (
    instrument_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    bar_start TEXT NOT NULL,
    close REAL NOT NULL CHECK(close > 0),
    PRIMARY KEY(instrument_id,timestamp)
);
'''

QUOTE_TTL_SECONDS = 15 * 60
REFRESH_SECONDS = 60
MAX_SECURITIES = 120
MAX_BARS = 1000
REQUEST_BUDGET_SECONDS = 70
_lock = threading.Lock()
_next_refresh = 0.0


RefreshBusy = market_cache.RefreshBusy


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _sessions(meta, kind="regular"):
    """Use supplied regular sessions, including early closes and lunch breaks."""
    raw = meta.get('tradingPeriods')
    # With includePrePost=false Yahoo normally returns a list of session lists.
    if isinstance(raw, dict):
        raw = raw.get(kind)
    elif kind != 'regular':
        return []
    sessions = []
    for day in raw if isinstance(raw, list) else []:
        for item in day if isinstance(day, list) else [day]:
            if not isinstance(item, dict):
                continue
            start, end = number(item.get('start')), number(item.get('end'))
            if start is not None and end is not None and 0 < end - start <= 86400:
                sessions.append((start, end))
    return sorted(set(sessions))


def normalize_chart(body, expected, *, fetched_at=None, include_extended=False):
    """Keep verified completed hourly closes, in UTC and major currency units."""
    result = body['chart']['result'][0]
    if body['chart'].get('error'):
        raise ValueError('Public market data unavailable')
    meta = result['meta']
    unit = prices._currency(meta.get('currency'))
    currency = prices._major_currency(unit)
    accepted_types = {'stock': {'EQUITY'}, 'adr': {'EQUITY'}, 'etf': {'ETF'}, 'cef': {'EQUITY', 'ETF'}}
    if (meta.get('symbol') != expected['symbol']
            or meta.get('exchangeName') not in expected['exchanges']
            or currency != expected['currency']
            or meta.get('instrumentType') not in accepted_types.get(expected['asset_type'], set())
            or meta.get('dataGranularity') not in {'1h', '60m'}):
        raise ValueError('Hourly listing metadata did not match')
    epoch = number(meta.get('regularMarketTime'))
    if epoch is None or not prices._asof(epoch):
        raise ValueError('Market timestamp missing or invalid')
    try:
        zone = ZoneInfo(meta['exchangeTimezoneName'])
    except (KeyError, TypeError, ZoneInfoNotFoundError):
        raise ValueError('Exchange timezone missing') from None
    retrieved = datetime.fromisoformat(fetched_at.replace('Z', '+00:00')) if fetched_at else datetime.now(timezone.utc)
    if retrieved.tzinfo is None:
        retrieved = retrieved.replace(tzinfo=timezone.utc)
    available_until = min(epoch, retrieved.timestamp(), time.time())
    timestamps = result.get('timestamp')
    closes = result['indicators']['quote'][0].get('close')
    if not isinstance(timestamps, list) or not isinstance(closes, list) or len(timestamps) != len(closes):
        raise ValueError('Hourly price history incomplete')
    sessions = _sessions(meta)
    extended = [(a, b, label) for kind, label in [('pre', 'pre-market'), ('post', 'after-hours')]
                for a, b in _sessions(meta, kind)] if include_extended else []
    if include_extended and not sessions:
        raise ValueError('Session boundaries missing for extended pricing')
    point_sessions = {}
    multiplier = 0.01 if unit == 'GBp' else 1
    points = {}
    for raw_start, raw_close in zip(timestamps, closes):
        start, close = number(raw_start), number(raw_close)
        if start is None or start <= 0 or close is None or close <= 0:
            continue
        session = next(((a, b) for a, b in sessions if a <= start < b), None)
        extended_session = next(((a, b, label) for a, b, label in extended if a <= start < b), None)
        if session is None and extended_session:
            session = extended_session[:2]
        if sessions and session is None:
            # This also removes the last-tick row Yahoo can append at the
            # session close; it is not a new hour of price history.
            continue
        if include_extended and session and (start - session[0]) % 3600 != 0:
            continue  # An appended last-tick row is not a completed interval.
        end = min(start + 3600, session[1]) if session else start + 3600
        limit = min(retrieved.timestamp(), time.time()) if extended_session else available_until
        if end > limit or end < limit - 30 * 86400:
            continue
        point = {'timestamp': _iso(end), 'bar_start': _iso(start), 'close': close * multiplier}
        if point['timestamp'] in points and points[point['timestamp']] != point:
            raise ValueError('Conflicting hourly prices')
        points[point['timestamp']] = point
        point_sessions[point['timestamp']] = extended_session[2] if extended_session else 'regular'
    if not points:
        raise ValueError('No completed hourly bars')
    raw_splits = (result.get('events') or {}).get('splits') or {}
    if not isinstance(raw_splits, dict):
        raise ValueError('Split history incomplete')
    splits = {}
    for split in raw_splits.values():
        if not isinstance(split, dict):
            raise ValueError('Invalid split history')
        stamp = number(split.get('date'))
        numerator, denominator = number(split.get('numerator')), number(split.get('denominator'))
        if stamp is None or numerator is None or denominator is None or min(numerator, denominator) <= 0:
            raise ValueError('Invalid split history')
        if stamp > min(retrieved.timestamp(), time.time()):
            continue
        point = {'timestamp': _iso(stamp), 'date': datetime.fromtimestamp(stamp, zone).date().isoformat(),
                 'numerator': numerator, 'denominator': denominator}
        if point['timestamp'] in splits and splits[point['timestamp']] != point:
            raise ValueError('Conflicting split history')
        splits[point['timestamp']] = point
    ordered = [points[key] for key in sorted(points)][-MAX_BARS:]
    return {
        'instrument_id': expected['instrument_id'], 'symbol': expected['symbol'], 'currency': currency,
        'exchange': meta['exchangeName'], 'asset_type': expected['asset_type'],
        'accepted_exchanges': sorted(expected['exchanges']), 'identity_verified': True,
        'interval': '1h', 'range': '1mo', 'status': 'ok', 'error': None,
        'as_of': ordered[-1]['timestamp'], 'market_as_of': _iso(epoch),
        'fetched_at': retrieved.astimezone(timezone.utc).isoformat(), 'points': ordered,
        'source_url': 'https://finance.yahoo.com/quote/' + quote(expected['symbol'], safe='') + '/chart/',
        'split_adjusted': True, 'dividends_included': False,
        'splits': [splits[key] for key in sorted(splits)], 'split_events_complete': True,
        'timestamp_basis': 'completed_bar_close',
        'session_cache_version': 1, 'extended_hours': include_extended, 'point_sessions': {p['timestamp']: point_sessions[p['timestamp']] for p in ordered},
    }


def fetch_listing(expected, read=prices._get_json):
    url = ('https://query1.finance.yahoo.com/v8/finance/chart/' + quote(expected['symbol'], safe='')
           + '?range=1mo&interval=1h&events=splits&includePrePost=true')
    return normalize_chart(read(url), expected, include_extended=True)


def database(path=None):
    conn = db.connect(Path(path) if path is not None else db.DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def read_intraday(conn, *, instrument_ids=None):
    """Read a consistent caller-owned snapshot, without writes or network I/O."""
    owns_snapshot = not conn.in_transaction
    if owns_snapshot:
        conn.execute('BEGIN')
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'investment_intraday_cache', 'investment_intraday_bars'} <= tables:
            return {}
        ids = tuple(dict.fromkeys(instrument_ids)) if instrument_ids is not None else None
        if ids == ():
            return {}
        clause = ' WHERE instrument_id IN (' + ','.join('?' for _ in ids) + ')' if ids is not None else ''
        args, result = ids or (), {}
        for row in conn.execute('SELECT * FROM investment_intraday_cache' + clause, args):
            item = json.loads(row['metadata'])
            item.update(fetched_at=row['fetched_at'], attempted_at=row['attempted_at'],
                        error=row['refresh_error'], points=[])
            item['status'] = ('stale' if item.get('identity_verified') else 'unavailable') if row['refresh_error'] else item.get('status', 'unavailable')
            result[row['instrument_id']] = item
        for row in conn.execute('SELECT * FROM investment_intraday_bars' + clause + ' ORDER BY instrument_id,timestamp', args):
            item = result.get(row['instrument_id'])
            if item and item.get('identity_verified'):
                item['points'].append({'timestamp': row['timestamp'], 'bar_start': row['bar_start'], 'close': row['close'],
                                       **({'session': (item.get('point_sessions') or {}).get(row['timestamp'], 'regular')} if item.get('extended_hours') else {})})
        return result
    finally:
        if owns_snapshot:
            conn.rollback()


def _identity_matches(previous, expected):
    return (previous.get('symbol') == expected['symbol'] and previous.get('currency') == expected['currency']
            and previous.get('asset_type') == expected['asset_type']
            and previous.get('accepted_exchanges') == sorted(expected['exchanges']))


def refresh_intraday(path=None, *, portfolio=None, fetch=fetch_listing, enforce_cooldown=True):
    """Refresh current verified listings at most once per fifteen-minute TTL."""
    global _next_refresh
    if not _lock.acquire(blocking=False):
        raise RefreshBusy('Hourly prices are already refreshing.')
    try:
        if enforce_cooldown and time.monotonic() < _next_refresh:
            raise RefreshBusy('Wait a minute before refreshing hourly prices again.')
        _next_refresh = time.monotonic() + REFRESH_SECONDS
        conn = database(path)
        try:
            cached = read_intraday(conn)
        finally:
            conn.close()
        portfolio = portfolio if portfolio is not None else snaptrade_store.load_portfolio(path)
        instruments, unsupported, conflicts = {}, set(), set()
        for account in (portfolio or {}).get('accounts', []):
            for position in account.get('positions', {}).get('data', []):
                expected = market_cache.listing(position)
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
        now = datetime.now(timezone.utc)
        wanted, skipped = [], 0
        for identifier, expected in instruments.items():
            previous = cached.get(identifier, {})
            try:
                age = (now - datetime.fromisoformat(previous['attempted_at'])).total_seconds()
                fresh = _identity_matches(previous, expected) and previous.get('session_cache_version') == 1 and 0 <= age < QUOTE_TTL_SECONDS
            except (KeyError, TypeError, ValueError):
                fresh = False
            if fresh:
                skipped += 1
            else:
                wanted.append(expected)
        omitted = max(0, len(wanted) - MAX_SECURITIES)
        wanted = wanted[:MAX_SECURITIES]
        deadline = time.monotonic() + REQUEST_BUDGET_SECONDS

        def run(expected):
            try:
                if time.monotonic() >= deadline:
                    return expected, None
                result = fetch(expected)
                if not (result and _identity_matches(result, expected) and result.get('identity_verified')
                        and result.get('exchange') in expected['exchanges'] and result.get('points')):
                    return expected, None
                return expected, result
            except Exception:
                return expected, None

        with ThreadPoolExecutor(max_workers=4, thread_name_prefix='hourly-public-prices') as pool:
            results = list(pool.map(run, wanted))
        attempted_at = now.isoformat()
        updated = failed = retained = 0
        conn = database(path)
        try:
            for identifier in conflicts:
                if identifier in cached:
                    meta = {key: value for key, value in cached[identifier].items()
                            if key not in {'points', 'fetched_at', 'attempted_at', 'error'}}
                    meta['identity_verified'] = False
                    conn.execute('UPDATE investment_intraday_cache SET metadata=?,attempted_at=?,refresh_error=? WHERE instrument_id=?',
                                 (json.dumps(meta, allow_nan=False), attempted_at,
                                  'Conflicting listing identities prevent hourly valuation.', identifier))
            for expected, result in results:
                identifier = expected['instrument_id']
                if result:
                    keys = ('instrument_id', 'symbol', 'currency', 'exchange', 'asset_type', 'accepted_exchanges',
                            'identity_verified', 'interval', 'range', 'status', 'as_of', 'market_as_of',
                            'source_url', 'split_adjusted', 'dividends_included', 'splits', 'split_events_complete', 'timestamp_basis', 'extended_hours', 'point_sessions', 'session_cache_version')
                    meta = {key: result.get(key) for key in keys}
                    conn.execute('''INSERT INTO investment_intraday_cache VALUES(?,?,?,?,NULL)
                        ON CONFLICT(instrument_id) DO UPDATE SET metadata=excluded.metadata,
                        fetched_at=excluded.fetched_at,attempted_at=excluded.attempted_at,refresh_error=NULL''',
                                 (identifier, json.dumps(meta, allow_nan=False), result['fetched_at'], attempted_at))
                    # Replace the entire window so prices from different split
                    # adjustment bases can never coexist in one time series.
                    conn.execute('DELETE FROM investment_intraday_bars WHERE instrument_id=?', (identifier,))
                    conn.executemany('INSERT INTO investment_intraday_bars VALUES(?,?,?,?)',
                                     [(identifier, p['timestamp'], p['bar_start'], p['close']) for p in result['points']])
                    updated += 1
                else:
                    failed += 1
                    message = 'Hourly public prices could not be verified or retrieved for this listing.'
                    previous = cached.get(identifier)
                    if previous:
                        meta = {key: value for key, value in previous.items()
                                if key not in {'points', 'fetched_at', 'attempted_at', 'error'}}
                        # Preserve the old physical rows, but never let an old
                        # listing price stand in for a changed listing identity.
                        if not _identity_matches(previous, expected):
                            meta['identity_verified'] = False
                        conn.execute('UPDATE investment_intraday_cache SET metadata=?,attempted_at=?,refresh_error=? WHERE instrument_id=?',
                                     (json.dumps(meta, allow_nan=False), attempted_at, message, identifier))
                        retained += bool(previous.get('points'))
                    else:
                        meta = {'instrument_id': identifier, 'symbol': expected['symbol'], 'currency': expected['currency'],
                                'asset_type': expected['asset_type'], 'accepted_exchanges': sorted(expected['exchanges']),
                                'exchange': None, 'as_of': None, 'market_as_of': None, 'identity_verified': False,
                                'status': 'unavailable', 'interval': '1h', 'range': '1mo', 'source_url': None}
                        conn.execute('INSERT INTO investment_intraday_cache VALUES(?,?,NULL,?,?)',
                                     (identifier, json.dumps(meta, allow_nan=False), attempted_at, message))
            conn.commit()
        finally:
            conn.close()
        return {'attempted': len(wanted), 'updated': updated, 'failed': failed, 'retained': retained,
                'unsupported': len(unsupported), 'omitted': omitted, 'skipped': skipped, 'fetched_at': attempted_at,
                'interval': '1h', 'refresh_after_seconds': QUOTE_TTL_SECONDS,
                'message': 'Hourly public prices updated.' if not failed else 'Some hourly prices are unavailable; previous verified observations are retained.'}
    finally:
        _lock.release()

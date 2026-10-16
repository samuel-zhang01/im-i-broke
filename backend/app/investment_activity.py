"""Durable, allowlisted brokerage activities and explicit retrieval evidence.

Fetching every page in a requested interval establishes retrieval coverage only.
It cannot establish broker completeness, cash-flow reconciliation, or returns.
Read helpers never initialize tables, seed data, or call a provider.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
import re
import sqlite3

from . import db, snaptrade_models as models


SCHEMA = '''
CREATE TABLE IF NOT EXISTS snaptrade_activity_accounts (
    account_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    institution TEXT NOT NULL,
    is_paper INTEGER NOT NULL CHECK(is_paper IN (0,1)),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snaptrade_activities (
    account_id TEXT NOT NULL REFERENCES snaptrade_activity_accounts(account_id),
    activity_id TEXT NOT NULL,
    type TEXT,
    description TEXT,
    symbol TEXT,
    amount REAL,
    currency TEXT,
    quantity REAL,
    activity_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(account_id,activity_id)
);
CREATE INDEX IF NOT EXISTS ix_snaptrade_activity_account_date
ON snaptrade_activities(account_id,activity_at,activity_id);
CREATE TABLE IF NOT EXISTS snaptrade_activity_fetches (
    account_id TEXT NOT NULL REFERENCES snaptrade_activity_accounts(account_id),
    retrieved_at TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('refresh','cache_seed')),
    requested_start TEXT,
    requested_end TEXT,
    status TEXT NOT NULL,
    truncated INTEGER CHECK(truncated IN (0,1)),
    pages_fetched INTEGER,
    received_count INTEGER NOT NULL,
    archived_count INTEGER NOT NULL,
    skipped_count INTEGER NOT NULL,
    fully_fetched INTEGER NOT NULL CHECK(fully_fetched IN (0,1)),
    PRIMARY KEY(account_id,retrieved_at,source)
);
CREATE INDEX IF NOT EXISTS ix_snaptrade_activity_fetch_date
ON snaptrade_activity_fetches(account_id,retrieved_at);
'''
MAX_PAGE_SIZE = 200
MAX_COVERAGE_WINDOWS = 20
LIMITATIONS = [
    'Saved activities accumulate from observed responses; activity absent from a later response is not deleted.',
    'A fully fetched window means the requested pages were retrieved without errors or truncation. It does not certify all broker activity or reconciled cash flows.',
    'Earlier activity, transfers, corporate actions, fees and corrections may be missing. Personal return calculations remain unavailable from this archive alone.',
]


def _day(value):
    try:
        return date.fromisoformat(value).isoformat() if isinstance(value, str) else None
    except ValueError:
        return None


def _retrieved(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc).isoformat()
    except (AttributeError, TypeError, ValueError):
        return None


def _provider_id(row):
    identifier = models.text(row.get('id'))
    # Older normalized caches predate id_source. Their only generated IDs
    # followed this exact pattern. Never turn an array index into event identity.
    if not identifier or row.get('id_source') == 'generated':
        return None
    if row.get('id_source') != 'provider' and re.fullmatch(r'activity:\d+', identifier):
        return None
    return identifier


def record_portfolio(conn, portfolio, *, source='refresh'):
    """Called within the portfolio save transaction, before stale cache merging.

    Only fresh normalized rows cross this boundary. Cache seeding is explicit
    and always has unknown request coverage, even if newer cache fields exist.
    """
    if source not in {'refresh', 'cache_seed'}:
        raise ValueError('Choose a supported activity observation source.')
    retrieved_at = _retrieved(portfolio.get('retrieved_at'))
    if retrieved_at is None:
        return
    for account in portfolio.get('accounts', []):
        account_id = models.text(account.get('id'))
        if not account_id:
            continue
        conn.execute('''INSERT INTO snaptrade_activity_accounts VALUES(?,?,?,?,?,?)
            ON CONFLICT(account_id) DO UPDATE SET name=excluded.name,institution=excluded.institution,
            is_paper=excluded.is_paper,last_seen_at=excluded.last_seen_at
            WHERE excluded.last_seen_at>=snaptrade_activity_accounts.last_seen_at''',
            (account_id, models.account_name(account.get('name')),
             models.text(account.get('institution'), 'Brokerage'), int(account.get('is_paper') is True),
             retrieved_at, retrieved_at))
        section = models.obj(account.get('activities'))
        # Repeated startup must neither re-ingest old rows nor revise provenance.
        if source == 'cache_seed' and conn.execute(
                'SELECT 1 FROM snaptrade_activity_fetches WHERE account_id=? LIMIT 1', (account_id,)).fetchone():
            continue
        if conn.execute('SELECT 1 FROM snaptrade_activity_fetches WHERE account_id=? AND retrieved_at=? AND source=?',
                        (account_id, retrieved_at, source)).fetchone():
            continue
        rows = section.get('data') if isinstance(section.get('data'), list) else []
        # A retained section can mix old and newly observed rows. Without
        # per-row provenance, its earlier cached_at cannot date every event;
        # the saved portfolio retrieval is the observation we can establish.
        row_seen_at = retrieved_at
        fresh = rows if source == 'cache_seed' or not section.get('stale') else []
        accepted, skipped = 0, 0
        seen = set()
        for raw in fresh:
            row = models.obj(raw)
            identifier = _provider_id(row)
            if identifier is None:
                skipped += 1
                continue
            if identifier in seen:
                continue
            seen.add(identifier)
            accepted += 1
            conn.execute('''INSERT INTO snaptrade_activities
                (account_id,activity_id,type,description,symbol,amount,currency,quantity,activity_at,first_seen_at,last_seen_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id,activity_id) DO UPDATE SET
                type=excluded.type,description=excluded.description,symbol=excluded.symbol,
                amount=excluded.amount,currency=excluded.currency,quantity=excluded.quantity,
                activity_at=excluded.activity_at,last_seen_at=excluded.last_seen_at
                WHERE excluded.last_seen_at>=snaptrade_activities.last_seen_at''',
                (account_id, identifier, models.text(row.get('type')),
                 models.text(row.get('description'), '', 500), models.text(row.get('symbol')),
                 models.number(row.get('amount')), models.currency(row.get('currency')),
                 models.number(row.get('quantity')), models.timestamp(row.get('date')),
                 row_seen_at, row_seen_at))
        start = _day(section.get('requested_start')) if source == 'refresh' else None
        end = _day(section.get('requested_end')) if source == 'refresh' else None
        if not start or not end or start > end:
            start = end = None
        pages = section.get('pages_fetched')
        pages = pages if source == 'refresh' and isinstance(pages, int) and not isinstance(pages, bool) and pages >= 0 else None
        status = section.get('status')
        status = status if status in {'ok', 'empty', 'error', 'unsupported'} else 'unknown'
        truncated = (int(section['truncated']) if source == 'refresh' and pages is not None and pages > 0
                     and isinstance(section.get('truncated'), bool) else None)
        full = (source == 'refresh' and status in {'ok', 'empty'} and not section.get('stale')
                and start is not None and pages is not None and pages > 0 and truncated == 0
                and section.get('pagination_complete') is True and skipped == 0)
        conn.execute('INSERT INTO snaptrade_activity_fetches VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                     (account_id, retrieved_at, source, start, end, status, truncated, pages,
                      len(rows), accepted, skipped, int(full)))


@contextmanager
def _read_database(conn=None, *, path=None):
    if conn is not None:
        yield conn
        return
    target = Path(path) if path is not None else db.DB_PATH
    if not target.exists():
        yield None
        return
    connection = sqlite3.connect(target.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _exists(conn):
    return conn is not None and conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='snaptrade_activity_accounts'").fetchone() is not None


def _account(row):
    return {'id': row['account_id'], 'name': models.account_name(row['name']),
            'institution': row['institution'], 'is_paper': bool(row['is_paper'])}


def list_activity_accounts(conn=None, *, path=None):
    with _read_database(conn, path=path) as connection:
        if not _exists(connection):
            return []
        return [{**_account(row), 'stored_activities': row['stored_activities']} for row in connection.execute('''
            SELECT a.*,COUNT(t.activity_id) AS stored_activities FROM snaptrade_activity_accounts a
            LEFT JOIN snaptrade_activities t ON t.account_id=a.account_id
            GROUP BY a.account_id ORDER BY a.is_paper,a.institution,a.name,a.account_id''')]


def _empty_coverage(account_id):
    return {'account_id': account_id, 'is_paper': None, 'stored_activities': 0,
            'first_activity_at': None, 'last_activity_at': None, 'last_attempt_at': None,
            'last_request_status': None, 'last_request_truncated': None,
            'last_requested_start': None, 'last_requested_end': None,
            'last_fully_fetched_window': None, 'fully_fetched_windows': [],
            'fully_fetched_request_count': 0, 'coverage_windows_truncated': False,
            'partial_or_failed_requests': 0, 'cache_seed_count': 0, 'skipped_activity_rows': 0,
            'history_complete': False, 'limitations': list(LIMITATIONS)}


def list_activity_coverage(conn=None, *, path=None, account_ids=None):
    """Mapping keyed by exact source account; successful intervals are capped."""
    if account_ids == []:
        return {}
    requested = list(dict.fromkeys(account_ids)) if account_ids is not None else None
    if requested is not None and len(requested) > 100:
        raise ValueError('Choose at most 100 activity accounts.')
    with _read_database(conn, path=path) as connection:
        if not _exists(connection):
            return {key: _empty_coverage(key) for key in requested or []}
        if requested is None:
            requested = [row['account_id'] for row in connection.execute('SELECT account_id FROM snaptrade_activity_accounts ORDER BY account_id')]
        result = {}
        for key in requested:
            item = _empty_coverage(key)
            account = connection.execute('SELECT is_paper FROM snaptrade_activity_accounts WHERE account_id=?', (key,)).fetchone()
            if account:
                item['is_paper'] = bool(account['is_paper'])
            stats = connection.execute('''SELECT COUNT(*) AS n,MIN(activity_at) AS first_at,
                MAX(activity_at) AS last_at FROM snaptrade_activities WHERE account_id=?''', (key,)).fetchone()
            item.update(stored_activities=stats['n'], first_activity_at=stats['first_at'], last_activity_at=stats['last_at'])
            counts = connection.execute('''SELECT COALESCE(SUM(fully_fetched),0) AS full,
                COALESCE(SUM(source='refresh' AND fully_fetched=0),0) AS partial,
                COALESCE(SUM(source='cache_seed'),0) AS seeded,COALESCE(SUM(skipped_count),0) AS skipped
                FROM snaptrade_activity_fetches WHERE account_id=?''', (key,)).fetchone()
            item.update(fully_fetched_request_count=counts['full'], partial_or_failed_requests=counts['partial'],
                        cache_seed_count=counts['seeded'], skipped_activity_rows=counts['skipped'])
            latest = connection.execute('''SELECT * FROM snaptrade_activity_fetches WHERE account_id=? AND source='refresh'
                ORDER BY retrieved_at DESC LIMIT 1''', (key,)).fetchone()
            if latest:
                item.update(last_attempt_at=latest['retrieved_at'], last_request_status=latest['status'],
                            last_request_truncated=bool(latest['truncated']) if latest['truncated'] is not None else None,
                            last_requested_start=latest['requested_start'], last_requested_end=latest['requested_end'])
            windows = [{'start': row['requested_start'], 'end': row['requested_end'], 'retrieved_at': row['retrieved_at']}
                       for row in connection.execute('''SELECT requested_start,requested_end,retrieved_at
                           FROM snaptrade_activity_fetches WHERE account_id=? AND fully_fetched=1
                           ORDER BY retrieved_at DESC LIMIT ?''', (key, MAX_COVERAGE_WINDOWS))]
            item.update(fully_fetched_windows=windows, last_fully_fetched_window=windows[0] if windows else None,
                        coverage_windows_truncated=counts['full'] > MAX_COVERAGE_WINDOWS)
            if counts['seeded']:
                item['limitations'].append('Existing cached activities were preserved at startup. Their earlier requested interval was not recorded and remains unknown.')
            if counts['skipped']:
                item['limitations'].append('Some observed rows lacked stable provider identifiers and could not safely be added to the durable archive.')
            result[key] = item
        return result


def saved_activities(account_id, *, conn=None, path=None, limit=50, offset=0):
    if not isinstance(account_id, str) or not account_id or len(account_id) > 240:
        raise ValueError('Choose an investment activity account.')
    if (not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_PAGE_SIZE
            or not isinstance(offset, int) or isinstance(offset, bool) or offset < 0):
        raise ValueError('Choose a valid activity page (1–200 rows and nonnegative offset).')
    with _read_database(conn, path=path) as connection:
        coverage = list_activity_coverage(connection, path=path, account_ids=[account_id])[account_id]
        account, rows = None, []
        if _exists(connection):
            stored_account = connection.execute('SELECT * FROM snaptrade_activity_accounts WHERE account_id=?', (account_id,)).fetchone()
            account = _account(stored_account) if stored_account else None
            rows = [dict(row) for row in connection.execute('''SELECT activity_id AS id,type,description,symbol,
                amount,currency,quantity,activity_at AS date,first_seen_at,last_seen_at FROM snaptrade_activities
                WHERE account_id=? ORDER BY activity_at DESC,activity_id LIMIT ? OFFSET ?''', (account_id, limit, offset))]
        return {'account': account, 'activities': rows, 'pagination': {'limit': limit, 'offset': offset,
                'total': coverage['stored_activities'], 'has_more': offset + len(rows) < coverage['stored_activities']},
                'coverage': coverage}

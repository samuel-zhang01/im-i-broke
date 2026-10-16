"""Private, normalized portfolio cache and observed history in Am I Broke?'s database.

Only the display-model allowlist below crosses the persistence boundary. Provider
responses, credentials, account numbers and signing data do not belong here.
History records observations; it does not imply prices existed on earlier dates.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path

from . import db
from . import snaptrade_models as models
from . import investment_activity


SCHEMA = """
CREATE TABLE IF NOT EXISTS snaptrade_cache (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    payload TEXT NOT NULL,
    saved_at TEXT NOT NULL,
    last_attempt_at TEXT NOT NULL,
    last_successful_refresh_at TEXT NOT NULL,
    refresh_error TEXT
);
CREATE TABLE IF NOT EXISTS snaptrade_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    retrieved_at TEXT NOT NULL UNIQUE,
    account_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS snaptrade_account_observations (
    snapshot_id INTEGER NOT NULL REFERENCES snaptrade_snapshots(id) ON DELETE CASCADE,
    account_id TEXT NOT NULL,
    name TEXT NOT NULL,
    institution TEXT NOT NULL,
    currency TEXT,
    total_value REAL,
    total_value_as_of TEXT,
    holdings_observed_at TEXT,
    is_paper INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    source_time_known INTEGER NOT NULL,
    holdings_status TEXT NOT NULL,
    holdings_truncated INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(snapshot_id, account_id)
);
CREATE INDEX IF NOT EXISTS ix_snaptrade_accounts_observed
ON snaptrade_account_observations(account_id, observed_at);
CREATE TABLE IF NOT EXISTS snaptrade_position_observations (
    snapshot_id INTEGER NOT NULL REFERENCES snaptrade_snapshots(id) ON DELETE CASCADE,
    account_id TEXT NOT NULL,
    position_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    asset_type TEXT,
    quantity REAL,
    price REAL,
    value REAL,
    currency TEXT,
    average_cost REAL,
    unrealized_pnl REAL,
    is_cash INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    source_time_known INTEGER NOT NULL,
    PRIMARY KEY(snapshot_id, account_id, position_id)
);
CREATE INDEX IF NOT EXISTS ix_snaptrade_positions_observed
ON snaptrade_position_observations(account_id, observed_at);
CREATE TABLE IF NOT EXISTS snaptrade_account_estimates (
    account_id TEXT NOT NULL,
    date TEXT NOT NULL,
    currency TEXT NOT NULL,
    total_value REAL NOT NULL,
    source TEXT NOT NULL CHECK(source = 'snaptrade_estimate'),
    retrieved_at TEXT NOT NULL,
    name TEXT NOT NULL,
    institution TEXT NOT NULL,
    is_paper INTEGER NOT NULL,
    PRIMARY KEY(account_id, date, currency)
);
"""

ACCOUNT_SECTIONS = ('balances', 'positions', 'orders', 'activities', 'history', 'return_rates')

ACCOUNT_FIELDS = {
    'id': models.text, 'name': models.account_name, 'institution': models.text,
    'number_suffix': lambda value: models.text(value, limit=4),
    'type': models.text, 'currency': models.currency, 'total_value': models.number,
    'total_value_as_of': models.timestamp,
    'connection_id': models.text, 'holdings_synced_at': models.timestamp,
    'activities_synced_at': models.timestamp, 'initial_sync_complete': models.boolean,
    'holdings_unavailable': lambda value: value is True,
    'is_paper': lambda value: value is True,
    'retrieved_at': models.timestamp, 'connection_scope': models.text,
}
ROW_FIELDS = {
    'balances': {'currency': models.currency, 'cash': models.number, 'buying_power': models.number},
    'positions': {
        'id': models.text, 'symbol': models.text, 'name': models.text,
        'instrument_id': models.text, 'raw_symbol': models.text, 'listing_currency': models.currency,
        'exchange': models.text, 'figi_code': models.text, 'figi_share_class': models.text,
        'multiplier': models.number,
        'asset_type': models.text, 'quantity': models.number, 'price': models.number,
        'value': models.number, 'currency': models.currency, 'average_cost': models.number,
        'unrealized_pnl': models.number, 'is_cash': lambda value: value is True,
    },
    'orders': {
        'id': models.text, 'symbol': models.text, 'action': models.text, 'status': models.text,
        'quantity': models.number, 'filled_quantity': models.number, 'limit_price': models.number,
        'execution_price': models.number, 'currency': models.currency,
        'placed_at': models.timestamp, 'updated_at': models.timestamp, 'type': models.text,
    },
    'activities': {
        'id': models.text, 'type': models.text,
        'id_source': lambda value: value if value in {'provider', 'generated'} else None,
        'description': lambda value: models.text(value, '', 500), 'symbol': models.text,
        'amount': models.number, 'currency': models.currency, 'quantity': models.number,
        'date': models.timestamp,
    },
    'connections': {
        'id': models.text, 'name': models.text, 'institution': models.text,
        'disabled': models.boolean, 'is_read_only': models.boolean, 'created_at': models.timestamp,
    },
    'history': {
        'date': models.timestamp, 'total_value': models.number, 'currency': models.currency,
        'source': lambda value: 'snaptrade_estimate',
    },
    'return_rates': {
        'timeframe': lambda value: value if value in models.RETURN_TIMEFRAMES else None,
        'return_percent': models.number, 'as_of': models.timestamp,
        'method': lambda value: 'unknown', 'source': lambda value: 'broker_reported',
    },
}


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def database(path=None):
    connection = db.connect(Path(path) if path is not None else db.DB_PATH)
    try:
        connection.executescript(SCHEMA + investment_activity.SCHEMA)
        from .product_connections import ensure_schema as ensure_connections
        ensure_connections(connection)
        snapshot_columns = {row[1] for row in connection.execute('PRAGMA table_info(snaptrade_snapshots)')}
        if 'connection_scope' not in snapshot_columns:
            connection.execute("ALTER TABLE snaptrade_snapshots ADD COLUMN connection_scope TEXT NOT NULL DEFAULT 'environment:snaptrade'")
        from .snapshot_retention import ensure_schema
        ensure_schema(connection)
        columns = {row[1] for row in connection.execute('PRAGMA table_info(snaptrade_account_observations)')}
        if 'holdings_truncated' not in columns:
            connection.execute('ALTER TABLE snaptrade_account_observations ADD COLUMN holdings_truncated INTEGER NOT NULL DEFAULT 0')
            connection.commit()
        for name in ('total_value_as_of', 'holdings_observed_at'):
            if name not in columns:
                # Old account observations used the positions clock. Preserve
                # those rows; absent independent total timing remains unknown.
                connection.execute(f'ALTER TABLE snaptrade_account_observations ADD COLUMN {name} TEXT')
        position_columns = {row[1] for row in connection.execute('PRAGMA table_info(snaptrade_position_observations)')}
        for name in ('instrument_id', 'raw_symbol', 'listing_currency', 'exchange', 'figi_code', 'figi_share_class', 'multiplier'):
            if name not in position_columns:
                connection.execute('ALTER TABLE snaptrade_position_observations ADD COLUMN ' + name + (' REAL' if name == 'multiplier' else ' TEXT'))
        connection.commit()
        yield connection
    finally:
        connection.close()


def project_row(raw, fields):
    return {key: convert(models.obj(raw).get(key)) for key, convert in fields.items()}


def clean_section(raw, kind):
    raw = models.obj(raw)
    status = raw.get('status')
    source_rows = raw.get('data') if isinstance(raw.get('data'), list) else []
    data = [project_row(row, ROW_FIELDS[kind]) for row in source_rows[:2000]
            if isinstance(row, dict)]
    if kind == 'return_rates':
        data = [row for row in data if row['timeframe'] and row['return_percent'] is not None][:6]
    result = {
        'status': status if status in {'ok', 'empty', 'error', 'unsupported'} else ('ok' if data else 'empty'),
        'data': data, 'error': models.text(raw.get('error'), limit=1000),
        'truncated': raw.get('truncated') is True,
        'as_of': models.timestamp(raw.get('as_of')),
        'stale': raw.get('stale') is True,
        'cached_at': models.timestamp(raw.get('cached_at')),
    }
    if kind == 'activities':
        pages = raw.get('pages_fetched')
        result.update(requested_start=investment_activity._day(raw.get('requested_start')),
                      requested_end=investment_activity._day(raw.get('requested_end')),
                      pages_fetched=pages if isinstance(pages, int) and not isinstance(pages, bool) and pages >= 0 else None,
                      pagination_complete=models.boolean(raw.get('pagination_complete')))
    return result


def clean_portfolio(raw):
    """Project again at the database boundary, even after SDK normalization."""
    raw = models.obj(raw)
    accounts = []
    seen = set()
    source_accounts = raw.get('accounts') if isinstance(raw.get('accounts'), list) else []
    for source in source_accounts[:30]:
        if not isinstance(source, dict):
            continue
        account = project_row(source, ACCOUNT_FIELDS)
        if not account['id'] or account['id'] in seen:
            continue
        seen.add(account['id'])
        account['name'] = account['name'] or 'Brokerage account'
        account['institution'] = account['institution'] or 'Brokerage'
        account.update({kind: clean_section(source.get(kind), kind)
                        for kind in ACCOUNT_SECTIONS})
        accounts.append(account)
    return {
        'configured': raw.get('configured') is True,
        'retrieved_at': models.timestamp(raw.get('retrieved_at')) or now(),
        'error': models.text(raw.get('error'), limit=1000),
        'accounts': accounts, 'connections': clean_section(raw.get('connections'), 'connections'),
    }


def selected_account_ids(accounts, account_ids=None, *, paper=False):
    """Validate aggregate selections while retaining per-provider read limits.

    A workspace has at most 50 saved connections plus the legacy environment
    connection, each with at most MAX_ACCOUNTS. Bound raw request size before
    deduplicating, and accept only identifiers present in the retained cache.
    """
    from .product_connections import MAX_SAVED_CONNECTIONS
    from .snaptrade_client import MAX_ACCOUNTS
    maximum = (MAX_SAVED_CONNECTIONS + 1) * MAX_ACCOUNTS
    requested = ([row['id'] for row in accounts if bool(row.get('is_paper')) == paper]
                 if account_ids is None else [] if account_ids == ['__none__'] else account_ids)
    valid = {row['id'] for row in accounts}
    if (not isinstance(requested, (list, tuple)) or len(requested) > maximum
            or any(not isinstance(value, str) or not 1 <= len(value) <= 240 or value not in valid
                   for value in requested)):
        raise ValueError('Choose valid connected accounts from the account selector.')
    return list(dict.fromkeys(requested))


def _has_stale_sections(payload):
    return payload['connections'].get('stale', False) or any(
        account[kind].get('stale', False)
        # Optional return summaries retain their own stale flag; failure does
        # not imply that the account balances or positions are out of date.
        for account in payload['accounts'] for kind in ACCOUNT_SECTIONS if kind != 'return_rates')


def _load_single(connection, connection_scope=None):
    row = (connection.execute('SELECT * FROM provider_connection_cache WHERE connection_id=?', (connection_scope,)).fetchone()
           if connection_scope else connection.execute('SELECT * FROM snaptrade_cache WHERE id=1').fetchone())
    if row is None:
        return None
    payload = clean_portfolio(json.loads(row['payload']))
    for account in payload['accounts']:
        account['retrieved_at'] = account.get('retrieved_at') or payload['retrieved_at']
        account['connection_scope'] = connection_scope or 'environment:snaptrade'
    payload['cache'] = {
        'saved_at': row['saved_at'], 'last_attempt_at': row['last_attempt_at'],
        'last_successful_refresh_at': row['last_successful_refresh_at'],
        'is_cached': True, 'stale': bool(row['refresh_error']) or _has_stale_sections(payload),
        'refresh_error': row['refresh_error'],
    }
    for account in payload['accounts']:
        account['connection_stale'] = bool(row['refresh_error']) or any(
            account[kind].get('stale') or account[kind].get('status') == 'error'
            or account[kind].get('truncated') for kind in ('positions', 'balances'))
    return payload


def _load(connection):
    """Combine display caches without inventing observations or freshness.

    Each connection saves observations independently. Account/section clocks
    remain attached to their source; the aggregate clock is display metadata.
    """
    legacy = _load_single(connection)
    payloads = [legacy] if legacy else []
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE name='provider_connection_cache'").fetchone()
    if exists:
        for row in connection.execute('''SELECT c.connection_id,p.enabled,p.status FROM provider_connection_cache c
                JOIN provider_connections p ON p.id=c.connection_id WHERE p.provider='snaptrade' ORDER BY p.created_at,p.id'''):
            payload = _load_single(connection, row['connection_id'])
            payload['configured'] = bool(row['enabled'] and row['status'] != 'disconnected')
            for account in payload['accounts']:
                account['connection_disconnected'] = row['status'] == 'disconnected'
            if not payload['configured']:
                payload['cache']['stale'] = True
                payload['cache']['refresh_error'] = 'This provider connection is paused or disconnected. Saved data remains available.'
                for account in payload['accounts']:
                    account['connection_stale'] = True
            payloads.append(payload)
    if not payloads:
        return None
    if len(payloads) == 1:
        return payloads[0]
    if legacy:
        from .snaptrade_client import configured
        if not configured():
            legacy['configured'] = False
            legacy['cache']['stale'] = True
            legacy['cache']['refresh_error'] = 'The server SnapTrade credentials are not configured. Its saved data remains available.'
            for account in legacy['accounts']:
                account['connection_stale'] = True
    errors = list(dict.fromkeys(p['error'] for p in payloads if p.get('error')))
    cache_errors = list(dict.fromkeys(p['cache']['refresh_error'] for p in payloads if p['cache'].get('refresh_error')))
    connections = {'status': 'ok', 'data': [row for p in payloads for row in p['connections']['data']],
                   'error': None, 'truncated': any(p['connections']['truncated'] for p in payloads),
                   'stale': any(p['connections'].get('stale') for p in payloads)}
    return {'configured': any(p['configured'] for p in payloads),
            'retrieved_at': max(p['retrieved_at'] for p in payloads), 'error': ' '.join(errors) or None,
            'accounts': [account for p in payloads for account in p['accounts']], 'connections': connections,
            'cache': {'saved_at': max(p['cache']['saved_at'] for p in payloads),
                      'last_attempt_at': max(p['cache']['last_attempt_at'] for p in payloads),
                      'last_successful_refresh_at': min(p['cache']['last_successful_refresh_at'] for p in payloads),
                      'is_cached': True, 'stale': any(p['cache']['stale'] for p in payloads),
                      'refresh_error': ' '.join(cache_errors) or None}}


def load_portfolio(path=None, connection_scope=None):
    with database(path) as connection:
        if connection_scope == 'environment:snaptrade':
            return _load_single(connection)
        return _load_single(connection, connection_scope) if connection_scope else _load(connection)


def _merge_section(incoming, previous, retrieved_at, previous_retrieved_at, *, kind=None):
    if incoming['status'] in {'ok', 'empty'}:
        incoming.update(stale=False, cached_at=retrieved_at)
    elif (kind in {'history', 'return_rates'} and incoming['status'] == 'error'
          and incoming['data'] and not incoming['stale']):
        # These normalizers validate each row independently. A bad sibling
        # leaves useful fresh rows alongside a visible section error. Keep
        # that subset: replacing it loses new evidence, while merging older
        # rows could resurrect a date/period rejected as conflicting. Earlier
        # history remains in the observation archive with its original date.
        incoming.update(stale=False, cached_at=retrieved_at)
    elif previous and previous.get('data'):
        # An interrupted activity page can contain some new rows. Retain those
        # and older cached rows, with an explicit mixed/stale provenance flag.
        fresh = incoming['data']
        if fresh and all(row.get('id') for row in fresh + previous['data']):
            seen = {row['id'] for row in fresh}
            incoming['data'] = (fresh + [row for row in previous['data'] if row['id'] not in seen])[:2000]
        else:
            incoming['data'] = previous['data']
        incoming.update(stale=True,
                        truncated=incoming['truncated'] or previous.get('truncated') is True,
                        cached_at=previous.get('cached_at') or previous_retrieved_at,
                        as_of=previous.get('as_of'))
    else:
        incoming.update(stale=False, cached_at=retrieved_at)
    return incoming


def _merge_previous(payload, previous):
    prior_accounts = {account['id']: account for account in previous['accounts']} if previous else {}
    prior_retrieved = previous['retrieved_at'] if previous else None
    for account in payload['accounts']:
        old_account = prior_accounts.get(account['id'], {})
        for kind in ACCOUNT_SECTIONS:
            _merge_section(account[kind], old_account.get(kind), payload['retrieved_at'], prior_retrieved,
                           kind=kind)
        if account['positions'].get('as_of'):
            # A failed live read can return an older daily discovery clock.
            # Keep the retained section's actual source time for all consumers.
            account['holdings_synced_at'] = account['positions']['as_of']
    _merge_section(payload['connections'], previous.get('connections') if previous else None,
                   payload['retrieved_at'], prior_retrieved)
    return payload


def save_portfolio(result, path=None, connection_scope=None):
    """Atomically save a successful account-list read and fresh observations.

    The caller must not pass an account-list failure. Error sections retain the
    last useful display data but never create a new holding observation.
    Repeating the same retrieval timestamp is idempotent for history.
    """
    payload = clean_portfolio(result)
    for account in payload['accounts']:
        if connection_scope and not account['id'].startswith(connection_scope + ':'):
            raise ValueError('Saved provider account identifiers must be scoped to their connection.')
        account['retrieved_at'] = payload['retrieved_at']
        account['connection_scope'] = connection_scope or 'environment:snaptrade'
    if not payload['configured'] or (payload['error'] and not payload['accounts']):
        raise ValueError('An unsuccessful portfolio read cannot replace the cache')
    with database(path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        scope = connection_scope or 'environment:snaptrade'
        incoming = {(account['id'].removeprefix(scope + ':') if connection_scope else account['id'], account['institution'])
                    for account in payload['accounts']}
        for previous in (_load(connection) or {}).get('accounts', []):
            previous_scope = previous.get('connection_scope') or 'environment:snaptrade'
            if previous_scope == scope or previous.get('connection_disconnected'):
                continue
            external_id = previous['id'].removeprefix(previous_scope + ':') if previous_scope != 'environment:snaptrade' else previous['id']
            if (external_id, previous['institution']) in incoming:
                raise ValueError('This brokerage account is already stored under another connection. Reconnect its original connection to preserve one financial history.')
        # Save the incoming response before display-only stale rows are merged.
        # A failed page may still contain valid new activities, while retained
        # cache rows must never gain a new observation timestamp.
        investment_activity.record_portfolio(connection, payload)
        payload = _merge_previous(payload, _load_single(connection, connection_scope))
        saved_at = now()
        retrieved_at = payload['retrieved_at']
        if connection_scope:
            connection.execute('''INSERT INTO provider_connection_cache
                (connection_id,payload,saved_at,last_attempt_at,last_successful_refresh_at,refresh_error)
                VALUES(?,?,?,?,?,NULL) ON CONFLICT(connection_id) DO UPDATE SET
                payload=excluded.payload,saved_at=excluded.saved_at,last_attempt_at=excluded.last_attempt_at,
                last_successful_refresh_at=excluded.last_successful_refresh_at,refresh_error=NULL''',
                (connection_scope, json.dumps(payload, separators=(',', ':'), allow_nan=False), saved_at, retrieved_at, retrieved_at))
        else:
            connection.execute('''INSERT INTO snaptrade_cache
            (id,payload,saved_at,last_attempt_at,last_successful_refresh_at,refresh_error)
            VALUES(1,?,?,?,?,NULL) ON CONFLICT(id) DO UPDATE SET
            payload=excluded.payload,saved_at=excluded.saved_at,last_attempt_at=excluded.last_attempt_at,
            last_successful_refresh_at=excluded.last_successful_refresh_at,refresh_error=NULL''',
                (json.dumps(payload, separators=(',', ':'), allow_nan=False), saved_at, retrieved_at, retrieved_at))
        inserted = connection.execute('''INSERT OR IGNORE INTO snaptrade_snapshots(retrieved_at,account_count,connection_scope)
                                         VALUES(?,?,?)''', (retrieved_at, len(payload['accounts']), connection_scope or 'environment:snaptrade'))
        if not inserted.rowcount:
            prior_scope = connection.execute('SELECT connection_scope FROM snaptrade_snapshots WHERE retrieved_at=?', (retrieved_at,)).fetchone()[0]
            if prior_scope != (connection_scope or 'environment:snaptrade'):
                raise ValueError('Two connection observations share an identical receipt timestamp. Retry the refresh to preserve both histories.')
        if inserted.rowcount:
            snapshot_id = inserted.lastrowid
            for account in payload['accounts']:
                positions = account['positions']
                total_as_of = account['total_value_as_of']
                total_observed_at = total_as_of or retrieved_at
                # A successful positions response may be newer than the total
                # returned by the account-list endpoint. Persist both clocks.
                source_at = positions.get('as_of') or account['holdings_synced_at']
                observed_at = source_at or retrieved_at
                connection.execute('''INSERT INTO snaptrade_account_observations
                    (snapshot_id,account_id,name,institution,currency,total_value,is_paper,observed_at,
                     retrieved_at,source_time_known,holdings_status,holdings_truncated,total_value_as_of,holdings_observed_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (snapshot_id, account['id'], account['name'], account['institution'],
                     account['currency'], account['total_value'], int(account['is_paper']), total_observed_at,
                     retrieved_at, int(total_as_of is not None), positions['status'], int(positions['truncated']),
                     total_as_of, source_at if not positions['stale'] else positions.get('as_of')))
                history = account['history']
                if history['status'] in {'ok', 'empty', 'error'} and not history['stale']:
                    for point in history['data']:
                        if not point['date'] or not point['currency'] or point['total_value'] is None:
                            continue
                        connection.execute('''INSERT INTO snaptrade_account_estimates
                            (account_id,date,currency,total_value,source,retrieved_at,name,institution,is_paper)
                            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id,date,currency) DO UPDATE SET
                            total_value=excluded.total_value,retrieved_at=excluded.retrieved_at,
                            name=excluded.name,institution=excluded.institution,is_paper=excluded.is_paper''',
                            (account['id'], point['date'], point['currency'], point['total_value'],
                             'snaptrade_estimate', retrieved_at, account['name'], account['institution'],
                             int(account['is_paper'])))
                if positions['status'] not in {'ok', 'empty'} or positions['stale']:
                    continue
                position_seen = set()
                for position in positions['data']:
                    if not position['id'] or position['id'] in position_seen:
                        continue
                    position_seen.add(position['id'])
                    connection.execute('''INSERT INTO snaptrade_position_observations
                        (snapshot_id,account_id,position_id,symbol,name,asset_type,quantity,price,value,
                         currency,average_cost,unrealized_pnl,is_cash,observed_at,retrieved_at,source_time_known)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (snapshot_id, account['id'], position['id'], position['symbol'] or 'Unknown',
                         position['name'] or 'Unknown security', position['asset_type'], position['quantity'],
                         position['price'], position['value'], position['currency'], position['average_cost'],
                         position['unrealized_pnl'], int(position['is_cash']), observed_at, retrieved_at,
                         int(source_at is not None)))
                    connection.execute('''UPDATE snaptrade_position_observations SET
                        instrument_id=?,raw_symbol=?,listing_currency=?,exchange=?,figi_code=?,figi_share_class=?,multiplier=?
                        WHERE snapshot_id=? AND account_id=? AND position_id=?''',
                        tuple(position.get(key) for key in ('instrument_id','raw_symbol','listing_currency','exchange','figi_code','figi_share_class','multiplier'))
                        + (snapshot_id, account['id'], position['id']))
        connection.commit()
        response = _load_single(connection, connection_scope)
        response['cache']['is_cached'] = False
        return response


def seed_activity_archive(path=None):
    """Controlled startup migration: preserve cached events without a sync.

    This explicit write is intentionally absent from GET handlers. Legacy cache
    contents establish observed events only, never retroactive request coverage.
    Repeated startup is harmless and leaves original observation dates intact.
    """
    with database(path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        scopes = [None, *[row[0] for row in connection.execute('SELECT connection_id FROM provider_connection_cache')]]
        for scope in scopes:
            cached = _load_single(connection, scope)
            if cached is not None:
                investment_activity.record_portfolio(connection, cached, source='cache_seed')
        connection.commit()


def record_refresh_failure(message, path=None):
    """Keep the last successful snapshot and record only a safe status message."""
    with database(path) as connection:
        connection.execute('UPDATE snaptrade_cache SET last_attempt_at=?,refresh_error=? WHERE id=1',
                           (now(), models.text(message, 'The portfolio could not be refreshed.', 1000)))
        connection.commit()
        return _load(connection)


def _account_record(row):
    record = dict(row)
    record['name'] = models.account_name(record.get('name'))
    # SQLite stores booleans as integers. Consumers use the normalized model's
    # strict boolean checks, including live/paper separation for archived links.
    record['is_paper'] = bool(record['is_paper'])
    if 'observed_at' in record:
        # Legacy rows have no independently retained total clock. Their saved
        # retrieval time establishes when Am I Broke? saw the total, not when the
        # broker valued it. Never borrow a positions timestamp for this value.
        total_as_of = record.get('total_value_as_of')
        record['observed_at'] = total_as_of or record['retrieved_at']
        record['source_time_known'] = int(total_as_of is not None)
    return record


def list_account_observations(path=None, *, account_ids=None, start=None, end=None):
    """Observed account totals with native currency and explicit source timing.

    Filters use observation time, inclusively. Full snapshot membership can be
    determined by snapshot_id; the caller decides how to handle missing totals,
    stale observations, paper accounts and conversion rates.
    """
    where, values = [], []
    if account_ids is not None:
        if not account_ids:
            return []
        where.append('account_id IN (' + ','.join('?' for _ in account_ids) + ')')
        values.extend(account_ids)
    if start:
        where.append('COALESCE(total_value_as_of,retrieved_at)>=?')
        values.append(start)
    if end:
        where.append('COALESCE(total_value_as_of,retrieved_at)<=?')
        values.append(end)
    clause = ' WHERE ' + ' AND '.join(where) if where else ''
    with database(path) as connection:
        return [_account_record(row) for row in connection.execute(
            'SELECT * FROM snaptrade_account_observations' + clause + ' ORDER BY COALESCE(total_value_as_of,retrieved_at),retrieved_at,account_id', values)]


def list_position_observations(path=None, *, account_id=None, start=None, end=None):
    where, values = [], []
    for column, operator, value in (('account_id', '=', account_id), ('observed_at', '>=', start),
                                    ('observed_at', '<=', end)):
        if value is not None:
            where.append(f'{column}{operator}?')
            values.append(value)
    clause = ' WHERE ' + ' AND '.join(where) if where else ''
    with database(path) as connection:
        return [dict(row) for row in connection.execute(
            'SELECT * FROM snaptrade_position_observations' + clause + ' ORDER BY observed_at,retrieved_at,account_id,position_id', values)]


def list_account_estimates(path=None, *, account_ids=None, start=None, end=None):
    where, values = [], []
    if account_ids is not None:
        if not account_ids:
            return []
        where.append('account_id IN (' + ','.join('?' for _ in account_ids) + ')')
        values.extend(account_ids)
    if start:
        where.append('date>=?')
        values.append(start)
    if end:
        where.append('date<=?')
        values.append(end)
    clause = ' WHERE ' + ' AND '.join(where) if where else ''
    with database(path) as connection:
        return [_account_record(row) for row in connection.execute(
            'SELECT * FROM snaptrade_account_estimates' + clause + ' ORDER BY date,account_id', values)]


def list_snapshots(path=None):
    """Only metadata is loaded to populate the historical holdings selector."""
    with database(path) as connection:
        return [dict(row) for row in connection.execute('''
            SELECT s.id,s.retrieved_at,s.account_count,s.connection_scope,COUNT(p.position_id) AS position_count
            FROM snaptrade_snapshots s LEFT JOIN snaptrade_position_observations p ON p.snapshot_id=s.id
            WHERE s.holdings_retained=1
            GROUP BY s.id ORDER BY s.retrieved_at DESC LIMIT 1000''')]


def get_snapshot(snapshot_id, path=None):
    with database(path) as connection:
        snapshot = connection.execute('SELECT * FROM snaptrade_snapshots WHERE id=?', (snapshot_id,)).fetchone()
        if snapshot is None or not snapshot['holdings_retained']:
            return None
        result = {'id': snapshot['id'], 'retrieved_at': snapshot['retrieved_at'], 'connection_scope': snapshot['connection_scope'], 'accounts': []}
        accounts = connection.execute('SELECT * FROM snaptrade_account_observations WHERE snapshot_id=? ORDER BY account_id',
                                      (snapshot_id,)).fetchall()
        for row in accounts:
            record = _account_record(row)
            account = {key: record[key] for key in ('name', 'institution', 'currency', 'total_value', 'total_value_as_of', 'observed_at')}
            account['name'] = models.account_name(account['name'])
            account.update(id=row['account_id'], is_paper=bool(row['is_paper']),
                           source_time_known=bool(record['source_time_known']))
            positions = []
            position_source_at = row['holdings_observed_at']
            for stored in connection.execute('''SELECT * FROM snaptrade_position_observations
                                              WHERE snapshot_id=? AND account_id=? ORDER BY position_id''',
                                             (snapshot_id, row['account_id'])):
                position = {key: stored[key] for key in ROW_FIELDS['positions'] if key != 'id'}
                position.update(id=stored['position_id'], is_cash=bool(stored['is_cash']))
                positions.append(position)
                if position_source_at is None and stored['source_time_known']:
                    position_source_at = stored['observed_at']
            account['positions'] = {
                'status': row['holdings_status'], 'data': positions,
                'error': None if row['holdings_status'] in {'ok', 'empty'} else 'Holdings were unavailable in this snapshot.',
                'truncated': bool(row['holdings_truncated']), 'as_of': position_source_at, 'stale': False,
                'cached_at': snapshot['retrieved_at'],
            }
            result['accounts'].append(account)
        return result

"""Read-only bank health and bounded, process-local request timing aggregates.

No financial amounts, merchant text, account identifiers, raw provider payloads,
network access or persistent monitoring tables are used by this module.
"""
from collections import Counter, deque
from datetime import datetime, timezone
import math
from threading import Lock
import time

from . import lunchflow_client
from .lunchflow_scheduler import sync_seconds

OPERATIONS = ('summary', 'account_detail', 'sync', 'mapping', 'review', 'health',
              'analytics', 'recurring', 'merchants', 'transactions', 'anomalies', 'planning',
              'transfers', 'advice')
SAMPLE_LIMIT = 128
STALE_SECONDS = 36 * 60 * 60
_started = time.monotonic()
_lock = Lock()
_timings = {}
_BANK_TABLES = ('bank_connections', 'bank_transactions', 'bank_balance_observations',
                'bank_transaction_revisions', 'bank_review_decisions', 'bank_sync_runs', 'bank_sync_lock')


def record_request(operation, status_code, elapsed_ms):
    """Accept a fixed operation label, HTTP status and elapsed milliseconds only."""
    if not isinstance(operation, str) or operation not in OPERATIONS:
        return False
    if isinstance(status_code, bool) or not isinstance(status_code, int) or not 100 <= status_code <= 599:
        return False
    if isinstance(elapsed_ms, bool) or not isinstance(elapsed_ms, (int, float)):
        return False
    if not math.isfinite(elapsed_ms) or not 0 <= elapsed_ms <= 86_400_000:
        return False
    with _lock:
        item = _timings.setdefault(operation, {'count': 0, 'total_ms': 0, 'max_ms': 0,
                                               'client_errors': 0, 'server_errors': 0,
                                               'recent': deque(maxlen=SAMPLE_LIMIT)})
        item['count'] += 1
        item['total_ms'] += elapsed_ms
        item['max_ms'] = max(item['max_ms'], elapsed_ms)
        item['client_errors'] += int(400 <= status_code < 500)
        item['server_errors'] += int(status_code >= 500)
        item['recent'].append(elapsed_ms)
    return True


def timing_snapshot():
    with _lock:
        operations = {}
        for operation, item in _timings.items():
            samples = sorted(item['recent'])
            percentile = lambda q: round(samples[max(0, math.ceil(q * len(samples)) - 1)], 3)
            operations[operation] = {
                'requests': item['count'], 'client_errors': item['client_errors'],
                'server_errors': item['server_errors'],
                'mean_ms': round(item['total_ms'] / item['count'], 3),
                'max_ms': round(item['max_ms'], 3), 'recent_samples': len(samples),
                'recent_p50_ms': percentile(.5), 'recent_p95_ms': percentile(.95),
            }
    return {'scope': 'Current server process; counters reset on restart.',
            'uptime_seconds': round(time.monotonic() - _started, 1),
            'sample_limit_per_operation': SAMPLE_LIMIT, 'operations': operations}


def _instant(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _age(value, current):
    instant = _instant(value)
    return round(max(0, (current - instant).total_seconds()), 1) if instant else None


def _public_stamp(value):
    instant = _instant(value)
    return instant.isoformat() if instant else None


def snapshot(conn, current=None):
    """SELECT-only health contract; accepts the caller's SQLite connection.

    Fetch age measures the app's cached observation, not the bank's underlying
    update time. Lunch Flow does not expose that timestamp in these responses.
    """
    current = current or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    saved_enabled = (conn.execute("SELECT COUNT(*) FROM provider_connections WHERE provider='lunchflow' AND enabled=1 AND secrets IS NOT NULL").fetchone()[0]
                     if 'provider_connections' in tables else 0)
    missing = [name for name in _BANK_TABLES if name not in tables]
    result = {'configured': lunchflow_client.configured() or bool(saved_enabled), 'schema_ready': not missing,
              'missing_tables': missing, 'checked_at': current.isoformat(),
              'timings': timing_snapshot(),
              'freshness_basis': 'Local fetch timestamps. The underlying bank update time is not provided.',
              'read_only': True}
    if missing:
        return result

    count = lambda sql: conn.execute(sql).fetchone()[0]
    statuses = dict(conn.execute('SELECT status,COUNT(*) FROM bank_transactions GROUP BY status'))
    # Status strings are fixed application enums, never emitted as arbitrary
    # response keys if a local cache was corrupted or edited externally.
    known_statuses = ('imported', 'matched', 'review', 'waiting_mapping', 'pending', 'native', 'ignored', 'expired')
    cache = {name: statuses.get(name, 0) for name in known_statuses}
    cache.update(total=sum(statuses.values()), unknown_status=sum(v for k, v in statuses.items() if k not in known_statuses),
                 revisions=count('SELECT COUNT(*) FROM bank_transaction_revisions'),
                 balance_observations=count('SELECT COUNT(*) FROM bank_balance_observations'),
                 review_decisions=count('SELECT COUNT(*) FROM bank_review_decisions'))
    result['cache'] = cache
    result['review_queue'] = {'awaiting_review': cache['review'], 'awaiting_mapping': cache['waiting_mapping'],
                              'pending_authorizations': cache['pending'], 'native_currency_records': cache['native']}
    account_counts = Counter(total=0, excluded=0, mapped=0, missing=0, inactive=0, failed=0,
                             unknown_currency=0, never_synced=0, no_balance=0, stale_balance=0)
    ages, balance_stamps = [], []
    invalid_timestamps = future_timestamps = 0
    for row in conn.execute("""SELECT c.excluded,c.account_id IS NOT NULL AS mapped,c.present,c.status,
            c.last_error IS NOT NULL AS failed,c.currency,c.last_synced_at,b.fetched_at
            FROM bank_connections c LEFT JOIN bank_balance_observations b
            ON b.id=(SELECT MAX(x.id) FROM bank_balance_observations x WHERE x.connection_id=c.id)"""):
        account_counts['total'] += 1
        account_counts['excluded'] += int(bool(row['excluded']))
        if row['excluded']:
            continue
        account_counts['mapped'] += row['mapped']
        account_counts['missing'] += int(not row['present'])
        account_counts['inactive'] += int(row['status'] != 'ACTIVE')
        account_counts['failed'] += row['failed']
        account_counts['unknown_currency'] += int(not row['currency'])
        sync_age = _age(row['last_synced_at'], current)
        account_counts['never_synced'] += int(sync_age is None)
        if sync_age is not None:
            ages.append(sync_age)
        balance_age = _age(row['fetched_at'], current)
        account_counts['no_balance'] += int(balance_age is None)
        account_counts['stale_balance'] += int(balance_age is None or balance_age > STALE_SECONDS
                                             or row['failed'] or not row['present'] or row['status'] != 'ACTIVE')
        if row['fetched_at'] and _instant(row['fetched_at']):
            balance_stamps.append(_instant(row['fetched_at']))
        for value in (row['last_synced_at'], row['fetched_at']):
            if value:
                parsed = _instant(value)
                invalid_timestamps += int(parsed is None)
                future_timestamps += int(parsed is not None and (parsed-current).total_seconds() > 300)
    result['connections'] = dict(account_counts)

    attempt = conn.execute('SELECT started_at,finished_at,status FROM bank_sync_runs ORDER BY id DESC LIMIT 1').fetchone()
    success = conn.execute("SELECT finished_at FROM bank_sync_runs WHERE status='success' ORDER BY id DESC LIMIT 1").fetchone()
    lock = conn.execute('SELECT owner,expires_at FROM bank_sync_lock WHERE singleton=1').fetchone()
    expires = _instant(lock['expires_at']) if lock else None
    saved_locks = (list(conn.execute('SELECT owner,expires_at FROM bank_provider_sync_locks WHERE owner IS NOT NULL'))
                   if 'bank_provider_sync_locks' in tables else [])
    saved_running = any(_instant(row['expires_at']) and _instant(row['expires_at']) > current for row in saved_locks)
    saved_expired = any(not _instant(row['expires_at']) or _instant(row['expires_at']) <= current for row in saved_locks)
    saved_failed = (conn.execute("SELECT COUNT(*) FROM provider_connections WHERE provider='lunchflow' AND enabled=1 AND last_error IS NOT NULL").fetchone()[0]
                    if 'provider_connections' in tables else 0)
    result['connections']['failed_provider_connections'] = saved_failed
    active_link = (' AND historical=0' if 'historical' in {row[1] for row in conn.execute('PRAGMA table_info(bank_transactions)')} else '')
    result['freshness'] = {
        'last_attempt_at': _public_stamp(attempt['started_at']) if attempt else None,
        'last_attempt_age_seconds': _age(attempt['started_at'], current) if attempt else None,
        'last_success_at': _public_stamp(success[0]) if success else None,
        'last_success_age_seconds': _age(success[0], current) if success else None,
        'latest_balance_fetched_at': max(balance_stamps).isoformat() if balance_stamps else None,
        'oldest_complete_account_sync_age_seconds': max(ages) if ages else None,
        'stale_after_seconds': STALE_SECONDS,
        'last_attempt_failed': bool(attempt and attempt['status'] == 'failed'),
    }
    interval = sync_seconds()
    result['scheduler'] = {'enabled': bool(result['configured'] and interval), 'interval_seconds': interval,
                           'running': bool(saved_running or (lock and lock['owner'] and expires and expires > current)),
                           'expired_lease': bool(saved_expired or (lock and lock['owner'] and (expires is None or expires <= current)))}
    result['integrity_issues'] = {
        'duplicate_provider_keys': count('SELECT COUNT(*) FROM (SELECT connection_id,provider_id FROM bank_transactions GROUP BY connection_id,provider_id HAVING COUNT(*)>1)'),
        'duplicate_ledger_links': count('SELECT COUNT(*) FROM (SELECT transaction_id FROM bank_transactions WHERE transaction_id IS NOT NULL' + active_link + ' GROUP BY transaction_id HAVING COUNT(*)>1)'),
        'orphan_connections': count('SELECT COUNT(*) FROM bank_transactions b LEFT JOIN bank_connections c ON c.id=b.connection_id WHERE c.id IS NULL'),
        'orphan_ledger_links': count('SELECT COUNT(*) FROM bank_transactions b LEFT JOIN transactions t ON t.id=b.transaction_id WHERE b.transaction_id IS NOT NULL AND t.id IS NULL'),
        'orphan_account_mappings': count('SELECT COUNT(*) FROM bank_connections c LEFT JOIN accounts a ON a.id=c.account_id WHERE c.account_id IS NOT NULL AND a.id IS NULL'),
        'wrong_account_links': count('SELECT COUNT(*) FROM bank_transactions b JOIN bank_connections c ON c.id=b.connection_id JOIN transactions t ON t.id=b.transaction_id WHERE c.account_id IS NULL OR c.account_id<>t.account_id'),
        'pending_ledger_links': count("SELECT COUNT(*) FROM bank_transactions WHERE is_pending=1 AND transaction_id IS NOT NULL AND status IN ('matched','imported')"),
        'foreign_currency_ledger_links': count("SELECT COUNT(*) FROM bank_transactions b JOIN bank_connections c ON c.id=b.connection_id WHERE b.transaction_id IS NOT NULL AND b.status IN ('matched','imported') AND (b.currency<>'GBP' OR c.currency<>'GBP')"),
        'invalid_account_timestamps': invalid_timestamps, 'future_account_timestamps': future_timestamps,
    }
    return result

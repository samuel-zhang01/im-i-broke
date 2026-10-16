"""Additive bank cache and conservative links to the statement ledger.

Raw observations and prior revisions are retained. Existing statement rows,
classifications, source files and overrides are never rewritten by a bank sync.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from . import lunchflow_client as client
from .db import DB_PATH, connect

SCHEMA = '''
CREATE TABLE IF NOT EXISTS bank_connections (
    id TEXT PRIMARY KEY, account_id TEXT REFERENCES accounts(id), name TEXT NOT NULL,
    institution_name TEXT NOT NULL, provider TEXT, currency TEXT, status TEXT,
    excluded INTEGER NOT NULL DEFAULT 0, present INTEGER NOT NULL DEFAULT 1,
    amount_multiplier INTEGER NOT NULL DEFAULT 1 CHECK(amount_multiplier IN (-1,1)),
    balance_multiplier INTEGER NOT NULL DEFAULT 1 CHECK(balance_multiplier IN (-1,1)),
    raw TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
    last_synced_at TEXT, last_error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_bank_local_mapping ON bank_connections(account_id) WHERE account_id IS NOT NULL AND excluded=0;
CREATE TABLE IF NOT EXISTS bank_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, connection_id TEXT NOT NULL REFERENCES bank_connections(id),
    provider_id TEXT NOT NULL, txn_date TEXT NOT NULL, amount REAL NOT NULL, currency TEXT NOT NULL,
    merchant TEXT, description TEXT, is_pending INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL, transaction_id INTEGER REFERENCES transactions(id), review_reason TEXT,
    raw TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
    historical INTEGER NOT NULL DEFAULT 0 CHECK(historical IN (0,1)),
    UNIQUE(connection_id,provider_id)
);
CREATE INDEX IF NOT EXISTS ix_bank_transaction_account ON bank_transactions(connection_id,txn_date);
CREATE UNIQUE INDEX IF NOT EXISTS ix_bank_transaction_link ON bank_transactions(transaction_id) WHERE transaction_id IS NOT NULL AND historical=0;
CREATE TABLE IF NOT EXISTS bank_connection_successors (
    predecessor_id TEXT PRIMARY KEY REFERENCES bank_connections(id),
    successor_id TEXT NOT NULL UNIQUE REFERENCES bank_connections(id),
    account_id TEXT NOT NULL REFERENCES accounts(id), confirmed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bank_transaction_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, bank_transaction_id INTEGER NOT NULL REFERENCES bank_transactions(id),
    raw TEXT NOT NULL, observed_at TEXT NOT NULL, UNIQUE(bank_transaction_id,raw)
);
CREATE TABLE IF NOT EXISTS bank_balance_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, connection_id TEXT NOT NULL REFERENCES bank_connections(id),
    amount REAL NOT NULL,currency TEXT NOT NULL,fetched_at TEXT NOT NULL,
    UNIQUE(connection_id,fetched_at)
);
CREATE TABLE IF NOT EXISTS bank_credit_rule_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id TEXT NOT NULL, account_id TEXT NOT NULL, currency TEXT NOT NULL,
    connection_first_seen_at TEXT NOT NULL, balance_type TEXT NOT NULL
        CHECK(balance_type IN ('available_credit','reported')),
    credit_limit REAL, confirmed_at TEXT NOT NULL,
    UNIQUE(connection_id,confirmed_at)
);
CREATE TABLE IF NOT EXISTS bank_credit_valuations (
    balance_observation_id INTEGER PRIMARY KEY REFERENCES bank_balance_observations(id),
    connection_id TEXT NOT NULL, account_id TEXT NOT NULL REFERENCES accounts(id),
    currency TEXT NOT NULL, effective_amount REAL NOT NULL,
    balance_type TEXT NOT NULL CHECK(balance_type IN ('available_credit','reported')),
    credit_limit REAL, rule_confirmed_at TEXT, fetched_at TEXT NOT NULL,
    evidence_kind TEXT NOT NULL CHECK(evidence_kind IN ('confirmed_rule_at_fetch','retrospective_attestation')),
    estimated INTEGER NOT NULL CHECK(estimated IN (0,1)),
    attested_at TEXT, scope_start TEXT, scope_end TEXT
);
CREATE INDEX IF NOT EXISTS ix_bank_credit_valuations_account_date
ON bank_credit_valuations(account_id,fetched_at);
CREATE TABLE IF NOT EXISTS bank_sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT,
    status TEXT NOT NULL, error TEXT, stats TEXT,
    provider_connection_id TEXT NOT NULL DEFAULT 'environment:lunchflow'
);
CREATE TABLE IF NOT EXISTS bank_derived_refresh_work (
    run_id INTEGER PRIMARY KEY REFERENCES bank_sync_runs(id), after_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bank_sync_lock (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), owner TEXT, expires_at TEXT, last_attempt_at TEXT
);
INSERT OR IGNORE INTO bank_sync_lock(singleton) VALUES(1);
CREATE TABLE IF NOT EXISTS bank_derived_refresh (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), after_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bank_provider_sync_locks (
 connection_id TEXT PRIMARY KEY, owner TEXT, expires_at TEXT, last_attempt_at TEXT
);
CREATE TABLE IF NOT EXISTS bank_review_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, bank_transaction_id INTEGER NOT NULL REFERENCES bank_transactions(id),
    action TEXT NOT NULL, transaction_id INTEGER, decided_at TEXT NOT NULL
);
'''
_SCHEMA_OBJECTS = frozenset(re.findall(r'CREATE (?:UNIQUE )?(?:TABLE|INDEX) IF NOT EXISTS (\w+)', SCHEMA))
_CURRENCY_CONFLICT = ('The provider reported a different account currency. '
                      'The established currency and saved history are preserved; review the connection.')


def now():
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(conn):
    # Individual execute calls preserve an enclosing caller transaction.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='bank_transactions'").fetchone():
        if 'historical' not in {row[1] for row in conn.execute('PRAGMA table_info(bank_transactions)')}:
            conn.execute('ALTER TABLE bank_transactions ADD COLUMN historical INTEGER NOT NULL DEFAULT 0 CHECK(historical IN (0,1))')
    for name, condition in (('ix_bank_local_mapping', 'excluded=0'), ('ix_bank_transaction_link', 'historical=0')):
        index = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if index and condition not in index[0].replace(' ', ''):
            conn.execute('DROP INDEX ' + name)
    for statement in SCHEMA.split(';'):
        if statement.strip():
            conn.execute(statement)
    columns = {row[1] for row in conn.execute('PRAGMA table_info(bank_connections)')}
    if 'provider_connection_id' not in columns:
        conn.execute("ALTER TABLE bank_connections ADD COLUMN provider_connection_id TEXT NOT NULL DEFAULT 'environment:lunchflow'")
    if 'external_id' not in columns:
        conn.execute('ALTER TABLE bank_connections ADD COLUMN external_id TEXT')
    conn.execute("UPDATE bank_connections SET external_id=id WHERE external_id IS NULL AND provider_connection_id='environment:lunchflow'")
    if 'provider_connection_id' not in {row[1] for row in conn.execute('PRAGMA table_info(bank_sync_runs)')}:
        conn.execute("ALTER TABLE bank_sync_runs ADD COLUMN provider_connection_id TEXT NOT NULL DEFAULT 'environment:lunchflow'")


def database(path=None):
    path = path or DB_PATH
    if not path.exists():
        raise ValueError('The Am I Broke? database is missing. Restore your backup before syncing.')
    conn = connect(path)
    try:
        existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
        columns = {row[1] for row in conn.execute('PRAGMA table_info(bank_connections)')}
        run_columns = {row[1] for row in conn.execute('PRAGMA table_info(bank_sync_runs)')}
        transaction_columns = {row[1] for row in conn.execute('PRAGMA table_info(bank_transactions)')}
        if (_SCHEMA_OBJECTS - existing or 'provider_connection_id' not in columns or 'external_id' not in columns
                or 'provider_connection_id' not in run_columns or 'historical' not in transaction_columns
                or not conn.execute('SELECT 1 FROM bank_sync_lock WHERE singleton=1').fetchone()):
            ensure_schema(conn)
            conn.commit()
        return conn
    except Exception:
        conn.close()
        raise


def has_cache(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bank_connections'").fetchone() is not None


def public_name(value):
    """Account titles can be IBANs; expose a useful masked suffix only."""
    value = str(value or '')
    value = re.sub(r'\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b', lambda m: 'Account •••• ' + re.sub(r'\s', '', m.group())[-4:], value, flags=re.I)
    return re.sub(r'\b\d{7,}\b', lambda m: '•••• ' + m.group()[-4:], value)


def bank_key(value):
    # Preserve non-Latin institution names: stripping them to an empty key
    # would make unrelated banks look like the same institution when linking.
    value = re.sub(r'[\W_]+', '', (value or '').casefold())
    for key in ('revolut','hsbc','lloyds','dbs','amex','americanexpress'):
        if key in value:
            return 'amex' if key in {'amex','americanexpress'} else key
    return value


def candidates(conn, account):
    mapped = {row[0] for row in conn.execute('SELECT account_id FROM bank_connections WHERE account_id IS NOT NULL AND excluded=0 AND id<>?', (account['id'],))}
    return [dict(row) for row in conn.execute('SELECT * FROM accounts WHERE account_type<>\'investment\'')
            if institution_matches(conn, row, account['institution_name']) and row['id'] not in mapped
            and (not account['currency'] or ('currency' not in row.keys()) or row['currency'] == account['currency'])]


def mapping_reason(conn, account):
    """Hold a possible reconnection rather than duplicate its saved history.

    Provider IDs may change when a connection is renewed. A missing or inactive
    mapped connection is therefore not evidence that its ledger account can be
    ignored while creating another account. Only an explicit choice may do so.
    """
    if account['account_id'] or account['excluded']:
        return None
    if account.get('external_id') and account.get('provider_connection_id'):
        duplicate = conn.execute('''SELECT 1 FROM bank_connections
            WHERE id<>? AND external_id=? AND provider_connection_id<>? AND account_id IS NOT NULL LIMIT 1''',
            (account['id'], account['external_id'], account['provider_connection_id'])).fetchone()
        if duplicate:
            return ('This provider account already appears in another connection. Reconnect the original connection '
                    'or review its mapping before adding another copy of the same financial history.')
    for previous in conn.execute("""SELECT * FROM bank_connections
            WHERE id<>? AND account_id IS NOT NULL AND excluded=0""", (account['id'],)):
        if (previous['present'] and previous['status'] == 'ACTIVE'
                and connection_state(conn, previous['provider_connection_id'])['enabled']):
            continue
        same_currency = not account['currency'] or not previous['currency'] or account['currency'] == previous['currency']
        if same_currency and bank_key(previous['institution_name']) == bank_key(account['institution_name']):
            return ('A previous connection from this bank is missing or inactive. '
                    'Review the original account before creating another account; '
                    'the new connection may contain the same history.')
    return None


def institution_matches(conn, account, institution):
    from .account_settings import effective_settings
    names = [account['bank'], *effective_settings(conn, account['id'])['aliases']]
    return any(bank_key(name) == bank_key(institution) for name in names)


def discover_accounts(conn, accounts, observed_at, provider_connection_id='environment:lunchflow'):
    # Discovery is authoritative only for this credential set. Legacy provider
    # IDs remain unchanged; saved connections use namespaced IDs upstream.
    ensure_schema(conn)
    conn.execute('UPDATE bank_connections SET present=0 WHERE provider_connection_id=?', (provider_connection_id,))
    for account in accounts:
        conn.execute('''INSERT INTO bank_connections(id,name,institution_name,provider,currency,status,excluded,raw,first_seen_at,last_seen_at)
            VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,
            institution_name=excluded.institution_name,provider=excluded.provider,
            currency=COALESCE(bank_connections.currency,excluded.currency),status=excluded.status,
            last_error=CASE WHEN bank_connections.currency IS NOT NULL AND excluded.currency IS NOT NULL
                AND bank_connections.currency<>excluded.currency THEN ? ELSE bank_connections.last_error END,
            raw=excluded.raw,last_seen_at=excluded.last_seen_at,present=1''',
            (account['id'],account['name'],account['institution_name'],account['provider'],account['currency'],account['status'],
             0,json.dumps(account['raw'],sort_keys=True),observed_at,observed_at,_CURRENCY_CONFLICT))
        conn.execute('UPDATE bank_connections SET provider_connection_id=?,external_id=? WHERE id=?',
                     (provider_connection_id, account.get('external_id', account['id']), account['id']))


def needs_balance_direction(account):
    savings = bool(re.search(r'\b(savings?|deposit)\b', account['name'], re.I))
    return ((bank_key(account['institution_name']) == 'amex' and not savings)
            or bool(re.search(r'\b(credit|loan|mortgage|charge card)\b', account['name'],re.I)))


def create_local_account(conn, account, account_type=None):
    if needs_balance_direction(account) and account_type != 'credit':
        raise ValueError('Choose a credit account type before adding this liability account.')
    if not account['currency']:
        raise ValueError('Account currency is not yet known. Sync its balance before creating a local account.')
    account_id = 'lunchflow:' + account['id']
    name = public_name(account['name']) or account['institution_name']
    account_type = account_type or ('savings' if 'saving' in name.lower() else 'cash')
    conn.execute('INSERT OR IGNORE INTO accounts(id,bank,account_type,display_name) VALUES(?,?,?,?)',
                 (account_id,account['institution_name'],account_type,name))
    if 'currency' in {row[1] for row in conn.execute('PRAGMA table_info(accounts)')}:
        conn.execute('UPDATE accounts SET currency=? WHERE id=?', (account['currency'],account_id))
    conn.execute('UPDATE bank_connections SET account_id=? WHERE id=?', (account_id,account['id']))
    return account_id


def record_account_result(conn, account_id, result, observed_at):
    account = dict(conn.execute('SELECT * FROM bank_connections WHERE id=?', (account_id,)).fetchone())
    # The raw account list is retained separately from the established currency.
    # Discovery must not relabel an existing ledger before this guard can run.
    listed = json.loads(account['raw'])
    listed_currency = client.currency(listed.get('currency')) if isinstance(listed,dict) else None
    currency_conflict = bool(account['currency'] and listed_currency and account['currency'] != listed_currency)
    if currency_conflict:
        result['errors'].append(_CURRENCY_CONFLICT)
    saved_balance_id = None
    if result['balance'] is not None:
        bal = result['balance']
        if account['currency'] and account['currency'] != bal['currency']:
            currency_conflict = True
            result['errors'].append('Account currency changed; the new balance is held for review.')
        elif not currency_conflict:
            conn.execute('UPDATE bank_connections SET currency=? WHERE id=?', (bal['currency'],account_id))
            conn.execute('INSERT OR IGNORE INTO bank_balance_observations(connection_id,amount,currency,fetched_at) VALUES(?,?,?,?)',
                         (account_id,bal['amount'],bal['currency'],observed_at))
            saved = conn.execute('''SELECT id,amount,currency FROM bank_balance_observations
                WHERE connection_id=? AND fetched_at=?''', (account_id,observed_at)).fetchone()
            if saved and saved['currency'] == bal['currency'] and Decimal(str(saved['amount'])) == Decimal(str(bal['amount'])):
                saved_balance_id = saved['id']
            account['currency'] = bal['currency']
    txns = result['transactions']
    if txns is not None:
        # Transaction currency can differ from the account denomination, even
        # when every transaction in this response uses the same currency. Keep
        # an unknown account unmapped until account/balance evidence supplies it.
        for row in txns:
            raw = json.dumps(row['raw'],sort_keys=True)
            previous = conn.execute('SELECT * FROM bank_transactions WHERE connection_id=? AND provider_id=?', (account_id,row['provider_id'])).fetchone()
            status = 'pending' if row['is_pending'] else 'waiting_mapping'
            reason = None
            if previous:
                changed = (previous['txn_date'] != row['date'] or previous['amount'] != row['amount'] or previous['currency'] != row['currency'] or bool(previous['is_pending']) != row['is_pending'])
                if previous['transaction_id'] and changed:
                    status, reason = 'review', 'The provider changed a linked transaction. The original ledger row is preserved.'
                elif previous['status'] == 'ignored':
                    status = 'ignored'
                elif previous['transaction_id']:
                    status,reason = previous['status'],previous['review_reason']
                if previous['raw'] != raw:
                    conn.execute('INSERT OR IGNORE INTO bank_transaction_revisions(bank_transaction_id,raw,observed_at) VALUES(?,?,?)', (previous['id'],previous['raw'],previous['last_seen_at']))
            if currency_conflict and not row['is_pending'] and status != 'ignored' and not (previous and previous['transaction_id']):
                status,reason = 'review', _CURRENCY_CONFLICT
            conn.execute('''INSERT INTO bank_transactions(connection_id,provider_id,txn_date,amount,currency,merchant,description,is_pending,status,review_reason,raw,first_seen_at,last_seen_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(connection_id,provider_id) DO UPDATE SET
                txn_date=excluded.txn_date,amount=excluded.amount,currency=excluded.currency,merchant=excluded.merchant,
                description=excluded.description,is_pending=excluded.is_pending,status=excluded.status,
                review_reason=excluded.review_reason,raw=excluded.raw,last_seen_at=excluded.last_seen_at''',
                (account_id,row['provider_id'],row['date'],row['amount'],row['currency'],row['merchant'],row['description'],int(row['is_pending']),status,reason,raw,observed_at,observed_at))
        # A complete read can expire authorizations that disappeared. Keep their
        # raw record; pending holds are never promoted by matching an amount.
        conn.execute("UPDATE bank_transactions SET status='expired' WHERE connection_id=? AND is_pending=1 AND last_seen_at<>? AND transaction_id IS NULL AND status='pending'", (account_id,observed_at))
    account = dict(conn.execute('SELECT * FROM bank_connections WHERE id=?', (account_id,)).fetchone())
    if (not currency_conflict and not account['account_id'] and not account['excluded'] and account['currency']
            and not needs_balance_direction(account) and not candidates(conn,account)
            and not mapping_reason(conn,account)):
        create_local_account(conn,account)
    if saved_balance_id is not None:
        from .bank_balance_rules import record_credit_valuation
        record_credit_valuation(conn, account_id, saved_balance_id)
    if not currency_conflict:
        reconcile_account(conn,account_id)
    complete = result['balance'] is not None and txns is not None and not result['errors']
    conn.execute('UPDATE bank_connections SET last_synced_at=CASE WHEN ? THEN ? ELSE last_synced_at END,last_error=? WHERE id=?',
                 (int(complete),observed_at,' '.join(dict.fromkeys(result['errors'])) or None,account_id))


def _normalized(value):
    return re.sub(r'[^a-z0-9]', '', str(value or '').casefold())


def _amount(value):
    return Decimal(str(value)).quantize(Decimal('0.01'))


def _labels(row):
    return {_normalized(row[key]) for key in ('description','merchant') if row[key] and len(_normalized(row[key])) >= 4}


def _same_label(left, right):
    return any(a == b or (min(len(a),len(b)) >= 8 and (a in b or b in a))
               for a in _labels(left) for b in _labels(right))


def candidate_transactions(conn, account, row, *, unlinked_only=False):
    low = (date.fromisoformat(row['txn_date']) - timedelta(days=7)).isoformat()
    high = (date.fromisoformat(row['txn_date']) + timedelta(days=7)).isoformat()
    amount = float(row['amount']) * account['amount_multiplier']
    return [dict(item) for item in conn.execute('''SELECT t.id,t.txn_date,t.description,t.merchant,t.amount,t.category
        FROM transactions t LEFT JOIN bank_transactions b ON b.transaction_id=t.id AND b.historical=0
        LEFT JOIN statements s ON s.id=t.statement_id
        WHERE t.account_id=? AND (t.txn_date BETWEEN ? AND ? OR (s.reconciled=1 AND
            (json_extract(CASE WHEN json_valid(t.meta) THEN t.meta ELSE '{}' END,'$.posting_date')=? OR
             json_extract(CASE WHEN json_valid(t.meta) THEN t.meta ELSE '{}' END,'$.posted_date')=?)))
        AND ABS(t.amount-?)<0.005
        AND NOT (t.statement_id IS NULL AND COALESCE(b.status,'')='imported' AND COALESCE(b.first_seen_at,'')=?)
        AND (?=0 OR b.transaction_id IS NULL)
        ORDER BY t.txn_date,t.id''', (account['account_id'],low,high,row['txn_date'],row['txn_date'],amount,row['first_seen_at'] if 'first_seen_at' in row.keys() else '',int(unlinked_only)))]


def _insert_transaction(conn, account, row):
    from .categorize import auto_category
    amount = row['amount'] * account['amount_multiplier']
    description = row['description'] or row['merchant'] or 'Bank transaction'
    label = client.merchant_label(row['merchant'],description)
    merchant = label['merchant']
    key = 'lf:' + hashlib.sha256((account['id'] + ':' + row['provider_id']).encode()).hexdigest()
    meta = {'source': 'lunchflow','provider_id': row['provider_id'],'currency': row['currency'],
            'source_merchant':merchant,'provider_merchant':row['merchant'],
            'merchant_source':label['merchant_source'],'amount_multiplier':account['amount_multiplier']}
    cursor = conn.execute('''INSERT INTO transactions(account_id,txn_date,description,merchant,source_merchant,amount,
        type_code,category,raw,meta,dedupe_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
        (account['account_id'],row['txn_date'],description,merchant,merchant,amount,'bank_feed',
         auto_category(description,merchant,amount),row['raw'],json.dumps(meta),key))
    conn.execute("UPDATE bank_transactions SET transaction_id=?,status='imported',review_reason=NULL WHERE id=?", (cursor.lastrowid,row['id']))
    return cursor.lastrowid


def reconcile_account(conn, account_id):
    account = dict(conn.execute('SELECT * FROM bank_connections WHERE id=?', (account_id,)).fetchone())
    if not account['account_id'] or account['excluded']:
        return
    rows = conn.execute("SELECT * FROM bank_transactions WHERE connection_id=? AND is_pending=0 AND transaction_id IS NULL AND status NOT IN ('ignored','expired') ORDER BY txn_date,id", (account_id,)).fetchall()
    eligible = []
    for row in rows:
        if row['currency'] != 'GBP' or account['currency'] != 'GBP':
            conn.execute("UPDATE bank_transactions SET status='native',review_reason=NULL WHERE id=?", (row['id'],))
        else:
            eligible.append(row)
    # Establish the complete candidate graph before linking either side. Exact
    # dates provide the strongest evidence; only then consider posting shifts.
    # Two identical repeated source rows cannot opportunistically consume a
    # statement row merely because another occurrence was linked first.
    matched = set()
    for exact_day in (True,False):
        linked = {row[0] for row in conn.execute('SELECT transaction_id FROM bank_transactions WHERE transaction_id IS NOT NULL AND historical=0')}
        graph = {}
        for row in eligible:
            if row['id'] in matched:
                continue
            graph[row['id']] = [item['id'] for item in candidate_transactions(conn,account,row)
                                if item['id'] not in linked and _same_label(item,row)
                                and (not exact_day or item['txn_date'] == row['txn_date'])]
        incoming = {}
        for targets in graph.values():
            for target in targets:
                incoming[target] = incoming.get(target,0)+1
        for source,targets in graph.items():
            if len(targets) == 1 and incoming[targets[0]] == 1:
                conn.execute("UPDATE bank_transactions SET transaction_id=?,status='matched',review_reason=NULL WHERE id=?", (targets[0],source))
                matched.add(source)
    for row in eligible:
        if row['id'] in matched:
            continue
        possible = candidate_transactions(conn,account,row)
        covered = conn.execute('SELECT 1 FROM statements WHERE account_id=? AND period_start<=? AND period_end>=? LIMIT 1', (account['account_id'],row['txn_date'],row['txn_date'])).fetchone()
        if possible or covered:
            reason = 'Possible statement overlap; confirm which transaction matches.' if possible else 'Inside an imported statement period; confirm this is an additional transaction.'
            conn.execute("UPDATE bank_transactions SET status='review',review_reason=? WHERE id=?", (reason,row['id']))
        else:
            _insert_transaction(conn,account,row)


def _admit_writer(conn):
    # Check mappings and reconciliation against the latest committed ledger.
    # A no-row UPDATE also admits callers already inside a deferred transaction.
    if conn.in_transaction:
        conn.execute('UPDATE bank_connections SET id=id WHERE 0')
    else:
        conn.execute('BEGIN IMMEDIATE')


def map_account(conn, provider_id, account_id=None, create_new=False, amount_multiplier=None, balance_multiplier=None, account_type=None,
                replace_connection_id=None, confirm_reconnection=False, confirm_distinct_account=False):
    _admit_writer(conn)
    account = conn.execute('SELECT * FROM bank_connections WHERE id=?', (provider_id,)).fetchone()
    if not account:
        raise LookupError('Bank account not found.')
    account = dict(account)
    if account['excluded']:
        raise ValueError('This connection is excluded. Existing statements remain available.')
    if replace_connection_id:
        if not confirm_reconnection or create_new or not account_id or account['account_id']:
            raise ValueError('Confirm that this renewed feed replaces the previous connection to the same saved account.')
        previous = conn.execute('SELECT * FROM bank_connections WHERE id=? AND account_id=? AND excluded=0',
                                (replace_connection_id, account_id)).fetchone()
        if not previous or previous['id'] == provider_id:
            raise ValueError('Choose the previous mapped bank connection for this saved account.')
        state = connection_state(conn, previous['provider_connection_id'])
        if previous['present'] and previous['status'] == 'ACTIVE' and state['enabled']:
            raise ValueError('The previous feed is still active. Pause it before replacing its connection.')
        if bank_key(previous['institution_name']) != bank_key(account['institution_name']) or previous['currency'] != account['currency']:
            raise ValueError('A renewed feed must use the same institution and currency as the previous connection.')
        if sync_state(conn, previous['provider_connection_id'])['running'] or sync_state(conn, account['provider_connection_id'])['running']:
            raise ValueError('Wait for bank syncing to finish before replacing a connection.')
        if amount_multiplier is not None and amount_multiplier != previous['amount_multiplier']:
            raise ValueError('Preserve the previous purchase direction when reconnecting saved history.')
        amount_multiplier = previous['amount_multiplier']
        from .bank_balance_rules import get_rule
        previous_rule = get_rule(conn, previous['id'])
        conn.execute('UPDATE bank_connections SET excluded=1 WHERE id=?', (previous['id'],))
        conn.execute('UPDATE bank_transactions SET historical=1 WHERE connection_id=?', (previous['id'],))
        conn.execute('INSERT INTO bank_connection_successors VALUES(?,?,?,?)', (previous['id'], provider_id, account_id, now()))
    elif confirm_reconnection:
        raise ValueError('Choose the previous connection before confirming a reconnection.')
    explicit_direction = balance_multiplier is not None
    amount_multiplier = account['amount_multiplier'] if amount_multiplier is None else amount_multiplier
    balance_multiplier = account['balance_multiplier'] if balance_multiplier is None else balance_multiplier
    if amount_multiplier not in (-1,1) or balance_multiplier not in (-1,1):
        raise ValueError('Amount and balance multipliers must be +1 or -1.')
    if account_type and account_type not in {'current','savings','credit','cash'}:
        raise ValueError('Unsupported account type.')
    linked = conn.execute('SELECT 1 FROM bank_transactions WHERE connection_id=? AND transaction_id IS NOT NULL LIMIT 1', (provider_id,)).fetchone()
    if linked and (account_id != account['account_id'] or amount_multiplier != account['amount_multiplier']):
        raise ValueError('This connection already has ledger transactions. Preserve its account mapping and amount direction.')
    if create_new:
        if mapping_reason(conn, account) and not confirm_distinct_account:
            raise ValueError('Reconnect the previous feed to its saved account instead of creating a duplicate account.')
        if ((needs_balance_direction(account) or account_type == 'credit')
                and (account_type != 'credit' or not explicit_direction)):
            raise ValueError('Choose a credit account type and confirm the balance direction before adding this liability account.')
        if account_id:
            raise ValueError('Choose an existing account or create a new one.')
        account_id = create_local_account(conn,account,account_type)
    else:
        target = conn.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
        if not target or target['account_type'] == 'investment':
            raise ValueError('Choose an existing bank account.')
        if not institution_matches(conn, target, account['institution_name']):
            raise ValueError('This account belongs to a different bank.')
        native = target['currency'] if 'currency' in target.keys() else 'GBP'
        if not account['currency'] or native != account['currency']:
            raise ValueError('The currencies of these accounts do not match.')
        if conn.execute('SELECT 1 FROM bank_connections WHERE account_id=? AND excluded=0 AND id<>?', (account_id,provider_id)).fetchone():
            raise ValueError('That local account is already linked to another bank connection.')
        liability = needs_balance_direction(account) or target['account_type'] == 'credit' or account_type == 'credit'
        if liability:
            if (account_type or target['account_type']) != 'credit':
                raise ValueError('Choose a credit account type before linking this liability account.')
            same_credit_mapping = account['account_id'] == account_id and target['account_type'] == 'credit'
            if not same_credit_mapping and not explicit_direction:
                raise ValueError('Confirm the balance direction before linking this credit account.')
    if account_type:
        target = conn.execute('SELECT account_type FROM accounts WHERE id=?', (account_id,)).fetchone()
        if target['account_type'] != account_type:
            # Mapping must not bypass the same history/type boundary enforced
            # by Settings and reinterpret cash statement balances as debt.
            from .account_settings import save_settings
            save_settings(conn, account_id, {'account_type': account_type})
    conn.execute('UPDATE bank_connections SET account_id=?,amount_multiplier=?,balance_multiplier=? WHERE id=?', (account_id,amount_multiplier,balance_multiplier,provider_id))
    if replace_connection_id and previous_rule:
        from .bank_balance_rules import inherit_rule
        inherit_rule(conn, provider_id, previous_rule)
    reconcile_account(conn,provider_id)


def resolve_review(conn, cache_id, action, transaction_id=None):
    _admit_writer(conn)
    row = conn.execute('SELECT * FROM bank_transactions WHERE id=?', (cache_id,)).fetchone()
    if not row:
        raise LookupError('Bank transaction not found.')
    account = dict(conn.execute('SELECT * FROM bank_connections WHERE id=?', (row['connection_id'],)).fetchone())
    if account['excluded'] or row['historical']:
        raise ValueError('This feed is retained as historical evidence; review its current connection instead.')
    # A provider can revise a previously booked, linked row back to pending.
    # It must remain possible to dismiss that revision while retaining the
    # original ledger evidence; it must never be imported or matched again.
    if row['status'] != 'review' or (row['is_pending'] and not (row['transaction_id'] and action == 'ignore')):
        raise ValueError('Only booked transactions awaiting review can be resolved here.')
    if row['transaction_id'] and action != 'ignore':
        raise ValueError('This provider revision is already linked. Keep the original ledger row by ignoring this revision.')
    if action != 'ignore' and (not account['account_id'] or row['currency'] != 'GBP' or account['currency'] != 'GBP'):
        raise ValueError('Link a GBP account before reviewing ledger matches.')
    if action == 'match':
        targets = candidate_transactions(conn,account,row)
        if transaction_id not in {item['id'] for item in targets}:
            raise ValueError('Choose a matching transaction from this account with the same amount.')
        if conn.execute('SELECT 1 FROM bank_transactions WHERE transaction_id=? AND historical=0', (transaction_id,)).fetchone():
            raise ValueError('That ledger transaction is already matched to a bank record.')
        conn.execute("UPDATE bank_transactions SET transaction_id=?,status='matched',review_reason=NULL WHERE id=?", (transaction_id,cache_id))
    elif action == 'import':
        transaction_id = _insert_transaction(conn,account,row)
    elif action == 'ignore':
        conn.execute("UPDATE bank_transactions SET status='ignored',review_reason=NULL WHERE id=?", (cache_id,))
    else:
        raise ValueError('Choose match, import or ignore.')
    conn.execute('INSERT INTO bank_review_decisions(bank_transaction_id,action,transaction_id,decided_at) VALUES(?,?,?,?)', (cache_id,action,transaction_id,now()))


def latest_balances(conn, *, resolve=True, today=None):
    if not has_cache(conn):
        return {}
    result = {}
    scope_column = 'c.provider_connection_id' if 'provider_connection_id' in {row[1] for row in conn.execute('PRAGMA table_info(bank_connections)')} else "'environment:lunchflow'"
    for row in conn.execute('''SELECT b.*,c.account_id,c.balance_multiplier,c.status,c.last_error,c.present,a.account_type,a.bank,
        ''' + scope_column + ''' AS provider_connection_id
        FROM bank_balance_observations b JOIN bank_connections c ON c.id=b.connection_id
        JOIN accounts a ON a.id=c.account_id
        WHERE c.account_id IS NOT NULL AND c.excluded=0 AND b.id=(SELECT MAX(x.id) FROM bank_balance_observations x WHERE x.connection_id=c.id)'''):
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row['fetched_at'])).total_seconds()
        connection = connection_state(conn, row['provider_connection_id'])
        result[row['account_id']] = {'amount':row['amount']*row['balance_multiplier'],'currency':row['currency'],
                                    'connection_id':row['connection_id'],
                                    'fetched_at':row['fetched_at'],'stale':age > 36*3600 or bool(row['last_error']) or not row['present'] or row['status'] != 'ACTIVE' or not connection['enabled'],
                                    'error':connection['error'] or row['last_error'],'status':row['status'],'source':'lunchflow'}
        if resolve and row['account_type'] == 'credit':
            from .balance_resolution import resolve_credit_balance
            balance = result[row['account_id']]
            reported, code, reported_as_of, source = balance['amount'], balance['currency'], row['fetched_at'], 'lunchflow'
            statement = conn.execute('''SELECT period_end,closing_balance FROM statements
                WHERE account_id=? AND period_end<=? AND closing_balance IS NOT NULL
                ORDER BY period_end DESC,id DESC LIMIT 1''',
                (row['account_id'], (today or datetime.now(timezone.utc).date()).isoformat())).fetchone()
            if statement and statement['period_end'] > row['fetched_at'][:10]:
                # A connection remains inspectable when its last observation
                # has been superseded by a newer imported card statement.
                reported, code, reported_as_of, source = -statement['closing_balance'], 'GBP', statement['period_end'], 'statement'
            valuation = resolve_credit_balance(conn, row['account_id'],
                reported_balance=reported, source=source, currency=code,
                reported_as_of=reported_as_of, institution=row['bank'], today=today)
            balance.update(amount=valuation['effective_value'], reported_amount=reported,
                           reported_source=source, provider_reported_amount=balance['amount'],
                           provider_currency=balance['currency'], currency=code,
                           valuation=valuation, balance_check=valuation['balance_check'],
                           as_of=valuation['effective_as_of'])
    return result


def balance_history(conn, account_id=None, limit=1000):
    if not has_cache(conn):
        return []
    sql = '''SELECT c.account_id,b.connection_id,b.amount*c.balance_multiplier AS amount,b.currency,b.fetched_at
        FROM bank_balance_observations b JOIN bank_connections c ON c.id=b.connection_id
        WHERE c.account_id IS NOT NULL AND c.excluded=0'''
    params = []
    if account_id is not None:
        sql += ' AND c.account_id=?'
        params.append(account_id)
    return [dict(row) for row in conn.execute(sql + ' ORDER BY b.fetched_at DESC LIMIT ?', (*params,limit))]


def credit_valuation_history(conn, account_id=None):
    """Immutable, source-bound card values observed under a confirmed rule."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bank_credit_valuations'").fetchone():
        return []
    query = '''SELECT account_id,connection_id,effective_amount,currency,balance_type,
        credit_limit,rule_confirmed_at,fetched_at,balance_observation_id,
        evidence_kind,estimated,attested_at,scope_start,scope_end
        FROM bank_credit_valuations'''
    params = ()
    if account_id is not None:
        query += ' WHERE account_id=?'
        params = (account_id,)
    return [dict(row) for row in conn.execute(query + ' ORDER BY fetched_at,balance_observation_id', params)]


def acquire_sync(conn, cooldown=60, provider_connection_id='environment:lunchflow'):
    stamp = now()
    current = datetime.fromisoformat(stamp)
    owner = uuid.uuid4().hex
    conn.execute('BEGIN IMMEDIATE')
    if provider_connection_id == 'environment:lunchflow':
        row = conn.execute('SELECT * FROM bank_sync_lock WHERE singleton=1').fetchone()
    else:
        conn.execute('INSERT OR IGNORE INTO bank_provider_sync_locks(connection_id) VALUES(?)', (provider_connection_id,))
        row = conn.execute('SELECT * FROM bank_provider_sync_locks WHERE connection_id=?', (provider_connection_id,)).fetchone()
    if row['owner'] and row['expires_at'] and row['expires_at'] > stamp:
        conn.rollback()
        raise ValueError('A bank sync is already running. Please wait.')
    if row['last_attempt_at'] and (current-datetime.fromisoformat(row['last_attempt_at'])).total_seconds() < cooldown:
        conn.rollback()
        raise ValueError('Please wait one minute between bank syncs.')
    values = (owner, (current + timedelta(minutes=10)).isoformat(), stamp)
    if provider_connection_id == 'environment:lunchflow':
        conn.execute('UPDATE bank_sync_lock SET owner=?,expires_at=?,last_attempt_at=? WHERE singleton=1', values)
    else:
        conn.execute('UPDATE bank_provider_sync_locks SET owner=?,expires_at=?,last_attempt_at=? WHERE connection_id=?', (*values, provider_connection_id))
    run_id = conn.execute("INSERT INTO bank_sync_runs(started_at,status,provider_connection_id) VALUES(?,'running',?)", (stamp,provider_connection_id)).lastrowid
    conn.commit()
    return owner,run_id


def finish_sync(conn, owner, run_id, error=None, stats=None):
    conn.execute('UPDATE bank_sync_runs SET finished_at=?,status=?,error=?,stats=? WHERE id=?', (now(),'failed' if error else 'success',error,json.dumps(stats or {}),run_id))
    conn.execute('UPDATE bank_sync_lock SET owner=NULL,expires_at=NULL WHERE singleton=1 AND owner=?', (owner,))
    conn.execute('UPDATE bank_provider_sync_locks SET owner=NULL,expires_at=NULL WHERE owner=?', (owner,))
    conn.commit()


def connection_state(conn, provider_connection_id):
    result = {'enabled': True, 'label': None, 'error': None}
    if provider_connection_id != 'environment:lunchflow' and conn.execute("SELECT 1 FROM sqlite_master WHERE name='provider_connections'").fetchone():
        row = conn.execute('SELECT enabled,label,status FROM provider_connections WHERE id=?', (provider_connection_id,)).fetchone()
        if row:
            result.update(enabled=bool(row['enabled']), label=row['label'])
            if not row['enabled']:
                result['error'] = 'This provider connection is paused or disconnected. Saved financial history remains available.'
    return result


def account_view(conn, row, *, balances=None):
    from .bank_balance_rules import get_rule
    account = dict(row)
    connection_scope = account.get('provider_connection_id', 'environment:lunchflow')
    connection = connection_state(conn, connection_scope)
    bal = conn.execute('SELECT amount,currency,fetched_at FROM bank_balance_observations WHERE connection_id=? ORDER BY id DESC LIMIT 1', (account['id'],)).fetchone()
    counts = {name:0 for name in ('cached','imported','matched','pending','review','native')}
    for item in conn.execute('SELECT status,COUNT(*) AS n FROM bank_transactions WHERE connection_id=? GROUP BY status', (account['id'],)):
        counts['cached'] += item['n']
        if item['status'] in counts:
            counts[item['status']] += item['n']
    potential = candidates(conn,account) if not account['account_id'] and not account['excluded'] else []
    reason = mapping_reason(conn,account)
    effective = (latest_balances(conn) if balances is None else balances).get(account['account_id'])
    balance = dict(bal) | {'amount':bal['amount']*account['balance_multiplier']} if bal else None
    if balance and effective:
        balance.update({key: effective[key] for key in ('amount', 'currency', 'reported_amount', 'reported_source',
            'provider_reported_amount', 'provider_currency', 'valuation', 'as_of') if key in effective})
    requires_direction = bool(not account['account_id'] and (needs_balance_direction(account)
        or (potential and all(item['account_type']=='credit' for item in potential))))
    if balance and requires_direction:
        # Keep provider evidence inspectable, but an unconfirmed card sign is
        # not an asset/debt value that any client should aggregate.
        balance.update(amount=None,reported_amount=balance['amount'],provider_reported_amount=bal['amount'])
    return {'id':account['id'],'account_id':account['account_id'],'name':public_name(account['name']),
            'provider_connection_id':connection_scope,'connection_label':connection['label'],'connection_enabled':connection['enabled'],
            'institution_name':account['institution_name'],'provider':account['provider'],'currency':account['currency'],
            'status':account['status'],'excluded':bool(account['excluded']),'present':bool(account['present']),
            'mapping_status':'excluded' if account['excluded'] else ('mapped' if account['account_id'] else ('review' if potential or reason else 'new')),
            'mapping_reason':reason,
            'mapping_candidates':[item['id'] for item in potential],
            'balance':balance, 'valuation': (effective or {}).get('valuation'),
            'balance_check': (effective or {}).get('balance_check'),
            'balance_rule':get_rule(conn, account['id']),
            'amount_multiplier':account['amount_multiplier'],'balance_multiplier':account['balance_multiplier'],
            'requires_balance_direction':requires_direction,
            'last_synced_at':account['last_synced_at'],'last_error':connection['error'] or account['last_error'],'counts':counts,
            'last_transaction_at':conn.execute('SELECT MAX(txn_date) FROM bank_transactions WHERE connection_id=? AND is_pending=0', (account['id'],)).fetchone()[0]}


def sync_state(conn, provider_connection_id=None):
    where, params = (' WHERE provider_connection_id=?', (provider_connection_id,)) if provider_connection_id else ('', ())
    attempted = conn.execute('SELECT * FROM bank_sync_runs' + where + ' ORDER BY id DESC LIMIT 1', params).fetchone()
    success = conn.execute("SELECT finished_at FROM bank_sync_runs WHERE status='success'" +
                           (' AND provider_connection_id=?' if provider_connection_id else '') + ' ORDER BY id DESC LIMIT 1', params).fetchone()
    if provider_connection_id and provider_connection_id != 'environment:lunchflow':
        lock = conn.execute('SELECT * FROM bank_provider_sync_locks WHERE connection_id=?', (provider_connection_id,)).fetchone()
    else:
        lock = conn.execute('SELECT * FROM bank_sync_lock WHERE singleton=1').fetchone()
    running = bool(lock and lock['owner'] and lock['expires_at'] > now())
    if provider_connection_id is None:
        running = running or bool(conn.execute('SELECT 1 FROM bank_provider_sync_locks WHERE owner IS NOT NULL AND expires_at>? LIMIT 1', (now(),)).fetchone())
    return {'last_attempt_at':attempted['started_at'] if attempted else None,'last_success_at':success[0] if success else None,
            'error':attempted['error'] if attempted else None,'running':running}


def summary(conn, *, today=None):
    from .lunchflow_scheduler import sync_seconds
    from .product_connections import sync_statuses
    from .currency_reporting import currency_precision
    effective_balances = latest_balances(conn, today=today)
    accounts = [account_view(conn,row, balances=effective_balances) for row in conn.execute('SELECT * FROM bank_connections ORDER BY institution_name,name,id')]
    totals, institutions, balance_totals = {}, {}, {}
    for account in accounts:
        institution = institutions.setdefault(account['institution_name'],{'name':account['institution_name'],'account_ids':[],'totals_by_currency':{}})
        institution['account_ids'].append(account['id'])
        if (account['balance'] and account['balance']['amount'] is not None and not account['excluded']
                and account['present'] and not account['requires_balance_direction']):
            bal = account['balance']
            breakdown = balance_totals.setdefault(bal['currency'], {'assets': 0.0, 'liabilities': 0.0, 'net': 0.0})
            breakdown['assets'] += max(0, bal['amount'])
            breakdown['liabilities'] += max(0, -bal['amount'])
            for target in (totals,institution['totals_by_currency']):
                target[bal['currency']] = target.get(bal['currency'],0)+bal['amount']
    totals = {code: round(amount, currency_precision(code)) for code, amount in totals.items()}
    for institution in institutions.values():
        institution['totals_by_currency'] = {code: round(amount, currency_precision(code))
                                            for code, amount in institution['totals_by_currency'].items()}
    local = []
    # planning uses latest_balances but never calls this summary, so no cycle.
    from .planning import account_snapshot
    snapshots = {row['id']:row for row in account_snapshot(conn, today)}
    linked = {account['account_id'] for account in accounts if not account['excluded']}
    for row in conn.execute('SELECT * FROM accounts WHERE account_type<>\'investment\' ORDER BY bank,display_name'):
        snap = snapshots.get(row['id'],{})
        local.append({'id':row['id'],'bank':row['bank'],'account_type':row['account_type'],'display_name':public_name(row['display_name']),
                      'connected':row['id'] in linked,'currency':row['currency'] if 'currency' in row.keys() else 'GBP',
                      'balance':snap.get('native_balance',snap.get('balance')),'balance_asof':snap.get('balance_asof'),'reconciled':snap.get('reconciled'),
                      'reported_balance':snap.get('reported_native_balance',snap.get('native_balance',snap.get('balance'))),
                      'valuation':snap.get('valuation'), 'balance_check':snap.get('balance_check'),
                      'balance_source':snap.get('balance_source'), 'balance_recorded_at':snap.get('balance_fetched_at'),
                      'n_txns':conn.execute('SELECT COUNT(*) FROM transactions WHERE account_id=?', (row['id'],)).fetchone()[0]})
    # Use the same credit evidence checks as net worth. Connected currency
    # totals net cash against debt; statement-only liabilities are separate.
    connected = {a['account_id']: a for a in accounts if a['account_id'] and not a['excluded']}
    statement_liabilities = {}
    statement_reported_liabilities = {}
    checks = []
    for account in local:
        provider = connected.get(account['id'])
        balance = provider.get('balance') if provider else None
        if not balance and account['balance'] is not None and account['balance'] < 0:
            code = account['currency'] or 'GBP'
            statement_liabilities[code] = statement_liabilities.get(code, 0) - account['balance']
        reported = account['reported_balance']
        if not balance and reported is not None and reported < 0:
            code = account['currency'] or 'GBP'
            statement_reported_liabilities[code] = statement_reported_liabilities.get(code, 0) - reported
        check = account.get('balance_check')
        if check and check.get('requires_review'):
            checks.append({'account_id': account['id'], 'name': account['display_name'], 'currency': account['currency'],
                           **check, 'valuation':account.get('valuation')})
    for code, breakdown in balance_totals.items():
        breakdown.update(assets=round(breakdown['assets'], currency_precision(code)),
                         liabilities=round(breakdown['liabilities'], currency_precision(code)), net=totals[code])
    statement_liabilities = {code: round(amount, currency_precision(code)) for code, amount in statement_liabilities.items()}
    statement_reported_liabilities = {code: round(amount, currency_precision(code)) for code, amount in statement_reported_liabilities.items()}
    stats = {'accounts':len(accounts),'mapped':sum(bool(a['account_id']) for a in accounts)}
    for field in ('cached','imported','matched','pending','review','native'):
        stats[field] = sum(a['counts'][field] for a in accounts)
    saved_configured = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name='provider_connections'").fetchone()
                            and conn.execute("SELECT 1 FROM provider_connections WHERE provider='lunchflow' AND enabled=1 AND secrets IS NOT NULL LIMIT 1").fetchone())
    return {'configured':client.configured() or saved_configured,'accounts':accounts,'institutions':list(institutions.values()),
            'connection_statuses': sync_statuses('lunchflow', conn=conn),
            'totals_by_currency':totals,'local_accounts':local,'stats':stats,
            'balance_totals_by_currency':balance_totals, 'balance_checks':checks,
            'statement_liabilities_by_currency':statement_liabilities,
            'statement_reported_liabilities_by_currency':statement_reported_liabilities,
            'estimated_accounts':sum(bool((a.get('valuation') or {}).get('estimated')) for a in local),
            'unknown_balance_accounts':sum(bool((a.get('valuation') or {}).get('current_unknown')) for a in local),
            'sync':sync_state(conn) | {'interval_seconds':sync_seconds()}}


def transaction_view(row, multiplier=1):
    fields = ('id','provider_id','amount','currency','merchant','description','status','transaction_id','review_reason')
    return ({field:row[field] for field in fields}
            | {'amount':row['amount']*multiplier,'date':row['txn_date'],'is_pending':bool(row['is_pending'])}
            | client.merchant_label(row['merchant'],row['description']))


def merchant_label_repairs(conn):
    """Read-only proposals for feed-only rows whose source label is a reference.

    Statement rows and explicit user reviews are excluded. Callers may update
    only source_merchant/meta and reclassify these IDs through saved rules;
    this function never mutates a row or replaces a corrected merchant/category.
    """
    if not has_cache(conn):
        return []
    proposals = []
    for row in conn.execute("""SELECT t.id,t.source_merchant,t.meta,b.merchant AS provider_merchant,b.description
            FROM transactions t JOIN bank_transactions b ON b.transaction_id=t.id AND b.historical=0
            WHERE t.type_code='bank_feed' AND t.statement_id IS NULL AND b.status='imported'
            AND COALESCE(t.category_source,'auto')<>'user'
            AND NOT EXISTS(SELECT 1 FROM overrides o WHERE o.dedupe_key=t.dedupe_key)"""):
        label = client.merchant_label(row['provider_merchant'],row['description'])
        if (label['merchant_source'] != 'description'
                or client.merchant_label_quality(row['source_merchant']) == 'usable'):
            continue
        try:
            meta = json.loads(row['meta'] or '{}')
        except (TypeError,ValueError):
            continue
        if not isinstance(meta,dict):
            continue
        meta.update(source_merchant=label['merchant'],provider_merchant=row['provider_merchant'],merchant_source='description')
        proposals.append({'transaction_id':row['id'],'source_merchant':label['merchant'],'meta':json.dumps(meta)})
    return proposals


def detail(conn, provider_id, limit=100, offset=0, status='all', q=None, date_from=None, date_to=None):
    account = conn.execute('SELECT * FROM bank_connections WHERE id=?', (provider_id,)).fetchone()
    if not account:
        raise LookupError('Bank account not found.')
    account = dict(account)
    clauses,params = ['connection_id=?'],[provider_id]
    if status == 'pending':
        clauses.append("status='pending'")
    elif status == 'review':
        clauses.append("status='review'")
    elif status == 'booked':
        clauses.append("is_pending=0 AND status NOT IN ('ignored','expired')")
    if q:
        clauses.append('(description LIKE ? ESCAPE \'\\\' OR merchant LIKE ? ESCAPE \'\\\')')
        query = '%' + q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_') + '%'
        params += [query,query]
    if date_from:
        clauses.append('txn_date>=?')
        params.append(str(date_from))
    if date_to:
        clauses.append('txn_date<=?')
        params.append(str(date_to))
    where = ' AND '.join(clauses)
    rows = conn.execute('SELECT * FROM bank_transactions WHERE ' + where + ' ORDER BY txn_date DESC,id DESC LIMIT ? OFFSET ?', (*params,limit,offset)).fetchall()
    total = conn.execute('SELECT COUNT(*) FROM bank_transactions WHERE ' + where, params).fetchone()[0]
    totals = {}
    for row in conn.execute("SELECT currency,amount FROM bank_transactions WHERE " + where + " AND status IN ('imported','matched','native')",params):
        item = totals.setdefault(row['currency'],{'money_in':0,'money_out':0})
        value = row['amount'] * account['amount_multiplier']
        item['money_in' if value >= 0 else 'money_out'] += abs(value)
    from .currency_reporting import currency_precision
    totals = {key:{k:round(v,currency_precision(key)) for k,v in value.items()} for key,value in totals.items()}
    reviews = []
    review_rows = rows if status == 'review' else conn.execute("SELECT * FROM bank_transactions WHERE connection_id=? AND status='review' ORDER BY txn_date DESC LIMIT 100", (provider_id,)).fetchall()
    for row in review_rows:
        # Reconciliation still considers claimed rows as overlap evidence.
        # The review picker must only offer matches the API can accept, and a
        # linked provider revision cannot be matched to a second ledger row.
        choices = candidate_transactions(conn,account,row,unlinked_only=True) if account['account_id'] and not row['transaction_id'] else []
        reviews.append(transaction_view(row,account['amount_multiplier']) | {'candidates':choices})
    history = [dict(row) for row in conn.execute('SELECT amount*? AS amount,currency,fetched_at FROM bank_balance_observations WHERE connection_id=? ORDER BY id DESC LIMIT 365', (account['balance_multiplier'],provider_id))]
    return {'account':account_view(conn,account),'transactions':[transaction_view(row,account['amount_multiplier']) for row in rows],
            'total':total,'limit':limit,'offset':offset,'totals_by_currency':totals,'balance_history':history,'reviews':reviews}


def list_balance_observations(conn):
    return balance_history(conn,limit=100000)


def match_statement_transaction(conn, account_id, txn_date, amount, description, merchant=None, used_ids=None, posting_date=None):
    """Return a feed ledger row only when statement evidence is unambiguous.

    Ingest must preserve its ID/key/classification and attach statement evidence;
    a None return means normal statement dedupe can continue. If an uncertain
    overlap exists, raise to roll back that upload instead of double counting.
    """
    if not has_cache(conn):
        return None
    low = (date.fromisoformat(txn_date)-timedelta(days=7)).isoformat()
    high = (date.fromisoformat(txn_date)+timedelta(days=7)).isoformat()
    candidates_ = conn.execute('''SELECT t.* FROM transactions t JOIN bank_transactions b ON b.transaction_id=t.id AND b.historical=0
        WHERE t.account_id=? AND b.is_pending=0 AND b.status IN ('imported','matched')
        AND (t.txn_date BETWEEN ? AND ? OR b.txn_date=?) AND ABS(t.amount-?)<0.005''', (account_id,low,high,posting_date,amount)).fetchall()
    candidates_ = [row for row in candidates_ if row['id'] not in (used_ids or set())]
    source = {'description':description,'merchant':merchant}
    exact = [row for row in candidates_ if _same_label(source,row)]
    if len(exact) == 1:
        return dict(exact[0])
    if candidates_:
        raise ValueError('This statement overlaps bank-feed transactions ambiguously. Existing records were preserved; review the bank feed before importing.')
    return None

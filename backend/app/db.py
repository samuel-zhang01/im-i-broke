"""SQLite storage layer for the finance app."""
from __future__ import annotations
import sqlite3
import os
from pathlib import Path

DB_PATH = Path(os.environ.get("FINANCE_DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "finance.db")))
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id            TEXT PRIMARY KEY,
    bank          TEXT NOT NULL,
    account_type  TEXT NOT NULL,
    sort_code     TEXT,
    account_number TEXT,
    display_name  TEXT
);

CREATE TABLE IF NOT EXISTS statements (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    TEXT NOT NULL REFERENCES accounts(id),
    source_file   TEXT UNIQUE NOT NULL,
    period_start  TEXT,
    period_end    TEXT,
    opening_balance REAL,
    closing_balance REAL,
    reconciled    INTEGER,
    meta          TEXT                     -- JSON: statement boundary/evidence provenance
);

CREATE TABLE IF NOT EXISTS transactions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    TEXT NOT NULL REFERENCES accounts(id),
    statement_id  INTEGER REFERENCES statements(id),
    txn_date      TEXT,
    description   TEXT,
    merchant      TEXT,
    source_merchant TEXT,
    merchant_kind TEXT DEFAULT 'unknown',
    spending_treatment TEXT DEFAULT 'normal',
    exclude_from_analysis INTEGER DEFAULT 0,
    classification TEXT,
    amount        REAL NOT NULL,           -- + money in, - money out
    currency      TEXT,                    -- NULL = legacy GBP; otherwise native currency
    balance       REAL,
    type_code     TEXT,
    category      TEXT,
    category_source TEXT DEFAULT 'auto',   -- auto | rule | user
    recurring_id  INTEGER,
    transfer_group TEXT,
    is_anomaly    INTEGER DEFAULT 0,
    raw           TEXT,
    meta          TEXT,                    -- JSON: parser extras (shares, isin, result…)
    dedupe_key    TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_txn_date ON transactions(txn_date);
CREATE INDEX IF NOT EXISTS ix_txn_merchant ON transactions(merchant);
CREATE INDEX IF NOT EXISTS ix_txn_account ON transactions(account_id);
CREATE INDEX IF NOT EXISTS ix_txn_account_date_amount ON transactions(account_id,txn_date,amount);

CREATE TABLE IF NOT EXISTS rules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern       TEXT NOT NULL,           -- substring/regex on description
    is_regex      INTEGER DEFAULT 0,
    category      TEXT NOT NULL,
    priority      INTEGER DEFAULT 100
);

CREATE TABLE IF NOT EXISTS anomaly_review_exceptions (
    dedupe_key TEXT PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS anomaly_reviews (
    dedupe_key TEXT PRIMARY KEY,
    outcome TEXT NOT NULL CHECK(outcome IN ('expected','issue')),
    snapshot TEXT NOT NULL,
    reviewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS overrides (
    dedupe_key    TEXT PRIMARY KEY,
    category      TEXT,
    merchant      TEXT,
    note          TEXT
);

CREATE TABLE IF NOT EXISTS merchants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'merchant',
    category TEXT,
    spending_treatment TEXT NOT NULL DEFAULT 'normal',
    source TEXT NOT NULL DEFAULT 'user',
    confidence REAL NOT NULL DEFAULT 1,
    source_url TEXT,
    notes TEXT,
    seed_key TEXT UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS merchant_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant_id INTEGER NOT NULL REFERENCES merchants(id) ON DELETE CASCADE,
    pattern TEXT NOT NULL,
    match_mode TEXT NOT NULL DEFAULT 'exact',
    context TEXT NOT NULL DEFAULT 'any',
    UNIQUE(merchant_id,pattern,match_mode,context)
);

CREATE TABLE IF NOT EXISTS merchant_suggestion_decisions (
    label_key TEXT NOT NULL,
    merchant_id INTEGER NOT NULL REFERENCES merchants(id) ON DELETE CASCADE,
    source_label TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('approved','dismissed')),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(label_key, merchant_id)
);

CREATE TABLE IF NOT EXISTS merchant_alias_approvals (
    alias_id INTEGER PRIMARY KEY REFERENCES merchant_aliases(id) ON DELETE CASCADE,
    approved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS merchant_alias_sources (
    alias_id INTEGER PRIMARY KEY REFERENCES merchant_aliases(id) ON DELETE CASCADE,
    source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS merchant_merge_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER NOT NULL REFERENCES merchants(id),
    source_ids TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_merchant_merge_target ON merchant_merge_history(target_id);

CREATE TABLE IF NOT EXISTS planning_scenarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    parameters TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS enrichment_cache (
    query_key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS preferences (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS data_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS recurring (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant      TEXT,
    cadence_days  REAL,
    typical_amount REAL,
    category      TEXT,
    first_seen    TEXT,
    last_seen     TEXT,
    n_occurrences INTEGER,
    status        TEXT,                    -- active | ended | price_change
    meta          TEXT
);

-- Cached live market prices (per ISIN) for portfolio valuation.
CREATE TABLE IF NOT EXISTS prices (
    isin          TEXT PRIMARY KEY,
    symbol        TEXT,
    currency      TEXT,
    price_native  REAL,
    price_gbp     REAL,
    updated       TEXT,
    quote_asof    TEXT,
    fetched_at    TEXT,
    fx_rate_to_gbp REAL,
    fx_asof       TEXT,
    provider      TEXT,
    source_url    TEXT,
    identity_verified INTEGER NOT NULL DEFAULT 0,
    quote_meta    TEXT
);

-- Public daily reference estimates, separate from actual broker costs.
CREATE TABLE IF NOT EXISTS reference_fx (
    date TEXT NOT NULL,
    currency TEXT NOT NULL,
    rate_to_gbp REAL NOT NULL CHECK(rate_to_gbp > 0),
    source TEXT NOT NULL,
    observed_date TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    source_url TEXT,
    PRIMARY KEY(date, currency)
);
CREATE TABLE IF NOT EXISTS fx_quotes (
    currency TEXT PRIMARY KEY,
    rate_to_gbp REAL NOT NULL CHECK(rate_to_gbp > 0),
    asof TEXT,
    fetched_at TEXT,
    source_url TEXT
);

-- Sourced non-cash changes to security units. Trade rows remain untouched.
CREATE TABLE IF NOT EXISTS corporate_actions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    TEXT NOT NULL REFERENCES accounts(id),
    isin          TEXT NOT NULL,
    action_type   TEXT NOT NULL CHECK(action_type = 'split'),
    effective_date TEXT NOT NULL,           -- issuer's legal effective date
    ex_date       TEXT NOT NULL,            -- before trading in adjusted units
    ratio_new     INTEGER NOT NULL CHECK(ratio_new > 0),
    ratio_old     INTEGER NOT NULL CHECK(ratio_old > 0),
    source_url    TEXT NOT NULL,
    source_title  TEXT NOT NULL,
    verified_at   TEXT NOT NULL,
    notes         TEXT,
    UNIQUE(account_id, isin, action_type, ex_date),
    CHECK(effective_date <= ex_date)
);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            conn.execute("PRAGMA journal_mode=MEMORY")
        conn.execute("PRAGMA foreign_keys=ON")
    except BaseException:
        conn.close()
        raise
    return conn


def init_db(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = connect(path)
    try:
        return _initialize_schema(conn)
    except BaseException:
        conn.close()
        raise


def _initialize_schema(conn: sqlite3.Connection) -> sqlite3.Connection:
    if conn.execute('PRAGMA user_version').fetchone()[0] > SCHEMA_VERSION:
        raise RuntimeError('This database requires a newer application release. Use matching images or restore the matching pre-upgrade backup to new storage.')
    conn.executescript(SCHEMA)
    account_columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
    if 'currency' not in account_columns:
        conn.execute("ALTER TABLE accounts ADD COLUMN currency TEXT NOT NULL DEFAULT 'GBP'")
    price_columns = {row[1] for row in conn.execute("PRAGMA table_info(prices)")}
    for name, definition in {
        'quote_asof': 'TEXT', 'fetched_at': 'TEXT', 'fx_rate_to_gbp': 'REAL',
        'fx_asof': 'TEXT', 'provider': 'TEXT', 'source_url': 'TEXT',
        'identity_verified': 'INTEGER NOT NULL DEFAULT 0', 'quote_meta': 'TEXT',
    }.items():
        if name not in price_columns:
            conn.execute(f"ALTER TABLE prices ADD COLUMN {name} {definition}")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(transactions)")}
    if "meta" not in columns:
        conn.execute("ALTER TABLE transactions ADD COLUMN meta TEXT")
    for name, definition in {
        'source_merchant': 'TEXT', 'merchant_kind': "TEXT DEFAULT 'unknown'",
        'spending_treatment': "TEXT DEFAULT 'normal'",
        'exclude_from_analysis': 'INTEGER DEFAULT 0', 'classification': 'TEXT',
        # NULL is the historical GBP ledger contract. New imports preserve
        # their explicit native currency instead of relabelling it as pounds.
        'currency': 'TEXT',
    }.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {name} {definition}")
    override_columns = {row[1] for row in conn.execute("PRAGMA table_info(overrides)")}
    for name, definition in {'merchant_kind': 'TEXT', 'spending_treatment': 'TEXT',
                             'exclude_from_analysis': 'INTEGER'}.items():
        if name not in override_columns:
            conn.execute(f"ALTER TABLE overrides ADD COLUMN {name} {definition}")
    merchant_columns = {row[1] for row in conn.execute("PRAGMA table_info(merchants)")}
    if 'confidence' not in merchant_columns:
        conn.execute('ALTER TABLE merchants ADD COLUMN confidence REAL NOT NULL DEFAULT 1')
    statement_columns = {row[1] for row in conn.execute("PRAGMA table_info(statements)")}
    if "meta" not in statement_columns:
        conn.execute("ALTER TABLE statements ADD COLUMN meta TEXT")
    from .corporate_actions import sync_known_actions
    sync_known_actions(conn)
    from .merchants import seed_merchants
    seed_merchants(conn)
    from .workspace import ensure_schema as ensure_workspace_schema
    ensure_workspace_schema(conn)
    from .account_settings import ensure_schema as ensure_account_settings
    from .account_coverage import ensure_schema as ensure_account_coverage
    from .product_imports import ensure_schema as ensure_product_imports
    from .currency_reporting import ensure_schema as ensure_currency_reporting
    ensure_account_settings(conn)
    ensure_account_coverage(conn)
    ensure_product_imports(conn)
    ensure_currency_reporting(conn)
    from .lunchflow_store import ensure_schema as ensure_banking_schema
    ensure_banking_schema(conn)
    from .product_connections import ensure_schema as ensure_connections
    ensure_connections(conn)
    conn.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
    conn.commit()
    return conn


def apply_startup_migrations(conn) -> bool:
    """Refresh derived labels once per classifier/catalog version on upgrade.

    Runs after additive schema migration, before API requests are served. A direct
    CLI import that leaves unclassified rows also triggers a refresh. An unchanged
    restart performs no writes or recomputation; saved decisions are reapplied.
    """
    import hashlib
    from .categorize import apply_categorization
    from .analytics import recompute_all
    catalog = Path(__file__).with_name('merchant_catalog.json')
    digest = hashlib.sha256(catalog.read_bytes() if catalog.exists() else b'').hexdigest()[:12]
    version = f'merchant-review-v3-feedback1:{digest}'
    applied = conn.execute('SELECT 1 FROM data_migrations WHERE version=?', (version,)).fetchone()
    incomplete = conn.execute('SELECT 1 FROM transactions WHERE classification IS NULL LIMIT 1').fetchone()
    if applied and not incomplete:
        return False
    apply_categorization(conn)
    recompute_all(conn, commit=False)
    conn.execute('INSERT OR IGNORE INTO data_migrations(version) VALUES(?)', (version,))
    conn.commit()
    return True

"""Persistent account choices independent of imports and provider identities.

Defaults reproduce the existing household. Settings never rewrite transaction
history, provider directions or source evidence.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import json
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from . import db

router = APIRouter(prefix='/api/workspace', tags=['Account settings'])
HEADERS = {'Cache-Control': 'no-store'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS account_settings (
 account_id TEXT PRIMARY KEY REFERENCES accounts(id), aliases TEXT NOT NULL DEFAULT '[]',
 include_in_reports INTEGER NOT NULL DEFAULT 1, include_in_planning INTEGER NOT NULL DEFAULT 1,
 archived INTEGER NOT NULL DEFAULT 0, ownership_share REAL NOT NULL DEFAULT 1 CHECK(ownership_share>0 AND ownership_share<=1),
 opened_on TEXT, closed_on TEXT, origin TEXT NOT NULL DEFAULT 'existing',
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS account_settings_history (
 id INTEGER PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
 previous TEXT NOT NULL, current TEXT NOT NULL, changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS manual_balance_observations (
 id INTEGER PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), amount REAL NOT NULL,
 currency TEXT NOT NULL, as_of TEXT NOT NULL, recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
'''


def ensure_schema(conn):
    for statement in SCHEMA.split(';'):
        if statement.strip():
            conn.execute(statement)


@contextmanager
def database():
    if not db.DB_PATH.exists():
        raise HTTPException(503, 'Set up or restore the database before managing accounts.')
    conn = db.connect(db.DB_PATH)
    try:
        ensure_schema(conn)
        conn.commit()
        yield conn
    finally:
        conn.close()


def effective_settings(conn, account_id):
    defaults = dict(aliases=[], include_in_reports=True, include_in_planning=True,
                    archived=False, ownership_share=1.0, opened_on=None, closed_on=None, origin='existing')
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='account_settings'").fetchone():
        row = conn.execute('SELECT * FROM account_settings WHERE account_id=?', (account_id,)).fetchone()
        if row:
            defaults.update({key: row[key] for key in defaults if key != 'aliases'})
            defaults['aliases'] = json.loads(row['aliases'])
    for key in ('include_in_reports', 'include_in_planning', 'archived'):
        defaults[key] = bool(defaults[key])
    return defaults


def account_view(conn, account_id):
    row = conn.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
    if not row:
        raise LookupError('Account not found.')
    account = dict(row)
    # Settings never expose full account numbers or sort codes.
    result = {key: account.get(key) for key in ('id', 'display_name', 'account_type')}
    result.update(institution=account['bank'], currency=account.get('currency') or 'GBP',
                  **effective_settings(conn, account_id))
    result['transaction_count'] = conn.execute('SELECT COUNT(*) FROM transactions WHERE account_id=?', (account_id,)).fetchone()[0]
    result['connections'] = []
    result['manual_balance'] = None
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='manual_balance_observations'").fetchone():
        balance = conn.execute('''SELECT id,amount,currency,as_of,recorded_at FROM manual_balance_observations
            WHERE account_id=? ORDER BY as_of DESC,id DESC LIMIT 1''', (account_id,)).fetchone()
        result['manual_balance'] = dict(balance) if balance else None
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='bank_connections'").fetchone():
        result['connections'] = [dict(r) for r in conn.execute('''SELECT id,name,amount_multiplier,balance_multiplier,present,excluded
            FROM bank_connections WHERE account_id=?''', (account_id,))]
    return result


class AccountPatch(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    account_type: Literal['current', 'savings', 'credit', 'cash', 'investment', 'debit'] | None = None
    institution: str | None = Field(default=None, min_length=1, max_length=200)
    aliases: list[str] | None = Field(default=None, max_length=30)
    include_in_reports: bool | None = None
    include_in_planning: bool | None = None
    archived: bool | None = None
    ownership_share: float | None = Field(default=None, gt=0, le=1, allow_inf_nan=False)
    opened_on: date | None = None
    closed_on: date | None = None
    currency: str | None = Field(default=None, pattern='^[A-Z]{3}$')


class ManualAccount(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    display_name: str = Field(min_length=1, max_length=200)
    institution: str = Field(default='Manual', min_length=1, max_length=200)
    account_type: Literal['current', 'savings', 'credit', 'cash', 'investment'] = 'cash'
    currency: str = Field(default='GBP', pattern='^[A-Z]{3}$')
    opened_on: date | None = None


class ManualBalance(BaseModel):
    model_config = ConfigDict(extra='forbid')
    amount: float = Field(allow_inf_nan=False, ge=-1e12, le=1e12)
    currency: str = Field(pattern='^[A-Z]{3}$')
    as_of: date


def preview_settings(conn, account_id, changes):
    before = account_view(conn, account_id)
    after = dict(before)
    for key, value in changes.items():
        if value is None and key not in ('opened_on', 'closed_on'):
            raise ValueError('Only account opening and closing dates can be cleared.')
        after[key] = value.isoformat() if isinstance(value, date) else value
    if 'currency' in changes:
        from .currency_reporting import currency_code
        after['currency'] = currency_code(after['currency'])
    aliases = after['aliases']
    if any(not isinstance(alias, str) or not 1 <= len(alias.strip()) <= 200 for alias in aliases):
        raise ValueError('Institution aliases must contain between 1 and 200 characters.')
    after['aliases'] = list(dict.fromkeys(alias.strip() for alias in aliases))
    if after['opened_on'] and after['closed_on'] and after['opened_on'] > after['closed_on']:
        raise ValueError('The opening date must be on or before the closing date.')
    blockers = []
    history = conn.execute('''SELECT COUNT(*),MIN(txn_date),MAX(txn_date) FROM transactions WHERE account_id=?''', (account_id,)).fetchone()
    statement_count = conn.execute('SELECT COUNT(*) FROM statements WHERE account_id=?', (account_id,)).fetchone()[0]
    has_sources = bool(history[0] or statement_count or before['connections'] or before['manual_balance'])
    if after['currency'] != before['currency'] and has_sources:
        blockers.append('An account with financial history or a source connection cannot change currency. Create a separate account for the new currency.')
    cash_types = {'current', 'savings', 'cash', 'debit'}
    if (after['account_type'] != before['account_type'] and has_sources
            and not {after['account_type'], before['account_type']} <= cash_types):
        blockers.append('An account with financial history or a source connection cannot change type here. Existing financial classifications and balances are preserved.')
    dates = [value for value in (history[1], history[2]) if value]
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'manual_balance_observations' in tables:
        dates.extend(value for value in conn.execute(
            'SELECT MIN(as_of),MAX(as_of) FROM manual_balance_observations WHERE account_id=?',
            (account_id,)).fetchone() if value)
    if {'bank_transactions', 'bank_connections'} <= tables:
        dates.extend(value for value in conn.execute('''SELECT MIN(b.txn_date),MAX(b.txn_date)
            FROM bank_transactions b JOIN bank_connections c ON c.id=b.connection_id
            WHERE c.account_id=? AND b.is_pending=0''', (account_id,)).fetchone() if value)
    if dates and after['opened_on'] and after['opened_on'] > min(dates):
        blockers.append('The opening date cannot be later than existing transactions or balance observations.')
    if dates and after['closed_on'] and after['closed_on'] < max(dates):
        blockers.append('The closing date cannot be earlier than existing transactions or balance observations.')
    return {'before': before, 'after': after, 'blockers': blockers, 'transaction_history_unchanged': True}


def save_settings(conn, account_id, changes):
    # Admit the writer before reading history or reconstructing the account.
    # An existing transaction may be a deferred read transaction; a no-row
    # UPDATE acquires write admission without changing financial evidence.
    if conn.in_transaction:
        conn.execute('UPDATE accounts SET id=id WHERE 0')
    else:
        conn.execute('BEGIN IMMEDIATE')
    ensure_schema(conn)
    preview = preview_settings(conn, account_id, changes)
    if preview['blockers']:
        raise ValueError(' '.join(preview['blockers']))
    result = preview['after']
    fields = ('include_in_reports', 'include_in_planning', 'archived', 'ownership_share', 'opened_on', 'closed_on', 'origin')
    conn.execute('''INSERT INTO account_settings(account_id,aliases,include_in_reports,include_in_planning,archived,ownership_share,opened_on,closed_on,origin)
        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET aliases=excluded.aliases,
        include_in_reports=excluded.include_in_reports,include_in_planning=excluded.include_in_planning,
        archived=excluded.archived,ownership_share=excluded.ownership_share,opened_on=excluded.opened_on,
        closed_on=excluded.closed_on,updated_at=CURRENT_TIMESTAMP''',
        (account_id, json.dumps(result['aliases']), *(result[key] for key in fields)))
    conn.execute('UPDATE accounts SET display_name=?,bank=?,account_type=? WHERE id=?',
                 (result['display_name'], result['institution'], result['account_type'], account_id))
    if 'currency' in {row[1] for row in conn.execute('PRAGMA table_info(accounts)')}:
        conn.execute('UPDATE accounts SET currency=? WHERE id=?', (result['currency'], account_id))
    elif result['currency'] != 'GBP':
        raise ValueError('Upgrade the database before adding other account currencies.')
    conn.execute('INSERT INTO account_settings_history(account_id,previous,current) VALUES(?,?,?)',
                 (account_id, json.dumps(preview['before'], sort_keys=True), json.dumps(result, sort_keys=True)))
    return account_view(conn, account_id)


def create_manual(conn, body):
    from .currency_reporting import currency_code
    currency_code(body.currency)
    ensure_schema(conn)
    account_id = 'manual:' + uuid.uuid4().hex
    conn.execute('INSERT INTO accounts(id,bank,account_type,display_name) VALUES(?,?,?,?)',
                 (account_id, body.institution, body.account_type, body.display_name))
    conn.execute("INSERT INTO account_settings(account_id,origin) VALUES(?,'manual')", (account_id,))
    return save_settings(conn, account_id, body.model_dump())


@router.get('/accounts')
def list_accounts():
    with database() as conn:
        return JSONResponse({'accounts': [account_view(conn, row[0]) for row in conn.execute('SELECT id FROM accounts ORDER BY display_name,id')]}, headers=HEADERS)


@router.post('/accounts', status_code=201)
def add_account(body: ManualAccount):
    with database() as conn:
        try:
            with conn:
                result = create_manual(conn, body)
            return JSONResponse(result, status_code=201, headers=HEADERS)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None


@router.post('/accounts/{account_id}/preview')
def preview_account(account_id: str, body: AccountPatch):
    with database() as conn:
        try:
            return JSONResponse(preview_settings(conn, account_id, body.model_dump(exclude_unset=True)), headers=HEADERS)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None


@router.patch('/accounts/{account_id}')
def update_account(account_id: str, body: AccountPatch):
    with database() as conn:
        try:
            with conn:
                result = save_settings(conn, account_id, body.model_dump(exclude_unset=True))
            return JSONResponse(result, headers=HEADERS)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None


@router.post('/accounts/{account_id}/balances', status_code=201)
def add_manual_balance(account_id: str, body: ManualBalance):
    with database() as conn:
        try:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                account = account_view(conn, account_id)
                if account['origin'] != 'manual':
                    raise ValueError('Manual balance observations are available for manual accounts only.')
                if body.currency != account['currency']:
                    raise ValueError('The balance currency must match the account currency.')
                from .workspace import reporting_today
                if body.as_of > reporting_today(conn):
                    raise ValueError('A balance observation cannot be dated in the future.')
                if (account['opened_on'] and body.as_of.isoformat() < account['opened_on']
                        or account['closed_on'] and body.as_of.isoformat() > account['closed_on']):
                    raise ValueError('The balance date is outside the account opening and closing dates.')
                conn.execute('INSERT INTO manual_balance_observations(account_id,amount,currency,as_of) VALUES(?,?,?,?)',
                             (account_id, body.amount, body.currency, body.as_of.isoformat()))
                result = account_view(conn, account_id)
            return JSONResponse(result, status_code=201, headers=HEADERS)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

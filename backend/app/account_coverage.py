"""Source-backed or explicitly confirmed history coverage, never inferred from sync."""
from __future__ import annotations

from contextlib import closing
from datetime import date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from . import db, workspace
from .account_settings import effective_settings

router = APIRouter(prefix='/api/workspace/accounts', tags=['Account coverage'])


def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS account_coverage_periods (
        id INTEGER PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
        period_start TEXT NOT NULL, period_end TEXT NOT NULL, complete INTEGER NOT NULL,
        source TEXT NOT NULL, evidence TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK(period_start<=period_end))''')


def state(conn, *, planning=True, account_id=None):
    accounts = []
    for row in conn.execute("SELECT * FROM accounts WHERE account_type<>'investment'"):
        if account_id is not None and row['id'] != account_id:
            continue
        settings = effective_settings(conn, row['id'])
        if settings['archived'] or not settings['include_in_reports'] or planning and not settings['include_in_planning']:
            continue
        accounts.append({key: row[key] for key in ('id', 'display_name', 'account_type')}
                        | {'currency': dict(row).get('currency') or 'GBP'} | settings)
    intervals = {a['id']: [] for a in accounts}
    gaps = {a['id']: [] for a in accounts}
    sources = set()
    for row in conn.execute('SELECT account_id,period_start,period_end FROM statements WHERE reconciled=1'):
        if row['account_id'] not in intervals or not row['period_start'] or not row['period_end']:
            continue
        try:
            left, right = date.fromisoformat(row['period_start']), date.fromisoformat(row['period_end'])
        except ValueError:
            continue
        if left > right:
            continue
        intervals[row['account_id']].append((left, right))
        sources.add('statement')
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='account_coverage_periods' AND type='table'").fetchone():
        for row in conn.execute('SELECT * FROM account_coverage_periods'):
            if row['account_id'] in intervals:
                target = intervals if row['complete'] else gaps
                target[row['account_id']].append((date.fromisoformat(row['period_start']), date.fromisoformat(row['period_end'])))
                sources.add(row['source'])
    return accounts, intervals, gaps, sorted(sources)


def required_period(account, start, end):
    left = max(start, date.fromisoformat(account['opened_on'])) if account.get('opened_on') else start
    right = min(end, date.fromisoformat(account['closed_on'])) if account.get('closed_on') else end
    return (left, right) if left <= right else None


def missing(accounts, intervals, gaps, start, end):
    from .planning import _covers
    required = [(account, period) for account in accounts if (period := required_period(account, start, end))]
    absent = [account['display_name'] for account, (left, right) in required
              if any(a <= right and b >= left for a, b in gaps.get(account['id'], []))
              or not _covers(intervals.get(account['id'], []), left, right)]
    return absent, len(required)


def _open():
    if not db.DB_PATH.is_file():
        raise HTTPException(503, 'The workspace database is unavailable.')
    conn = db.connect(db.DB_PATH)
    ensure_schema(conn)
    conn.commit()
    return conn


def _account(conn, ident):
    if not conn.execute('SELECT 1 FROM accounts WHERE id=?', (ident,)).fetchone():
        raise HTTPException(404, 'Account not found.')


class CoverageInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    period_start: date
    period_end: date
    complete: StrictBool
    evidence: str = Field(min_length=8, max_length=1000)


@router.get('/{ident}/coverage')
def list_coverage(ident: str):
    with closing(_open()) as conn:
        _account(conn, ident)
        return {'periods': [dict(row) | {'complete': bool(row['complete'])} for row in conn.execute(
            'SELECT * FROM account_coverage_periods WHERE account_id=? ORDER BY period_start,id', (ident,))]}


@router.post('/{ident}/coverage', status_code=201)
def add_coverage(ident: str, body: CoverageInput):
    with closing(_open()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        if body.period_start > body.period_end or body.period_end > workspace.reporting_today(conn):
            raise HTTPException(422, 'Coverage must be an ordered period ending today or earlier.')
        _account(conn, ident)
        cur = conn.execute('INSERT INTO account_coverage_periods(account_id,period_start,period_end,complete,source,evidence) VALUES(?,?,?,?,?,?)',
                           (ident, body.period_start.isoformat(), body.period_end.isoformat(), int(body.complete), 'user_confirmed', body.evidence))
        return dict(conn.execute('SELECT * FROM account_coverage_periods WHERE id=?', (cur.lastrowid,)).fetchone())


@router.delete('/{ident}/coverage/{period_id}')
def delete_coverage(ident: str, period_id: int):
    with closing(_open()) as conn, conn:
        _account(conn, ident)
        if not conn.execute('DELETE FROM account_coverage_periods WHERE account_id=? AND id=?', (ident, period_id)).rowcount:
            raise HTTPException(404, 'Coverage period not found.')
        return {'ok': True}

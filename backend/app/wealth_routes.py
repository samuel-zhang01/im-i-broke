"""Private local wealth reads and explicit public FX refresh."""
import threading
import time
from contextlib import closing

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from . import db, wealth, source_reconciliation
from .investment_read_cache import cached_json
from .account_selection import WealthAccountSelection
from .workspace import reporting_today

router = APIRouter(prefix='/api/wealth', tags=['Net worth'])
_refresh_lock = threading.Lock()
_next_refresh = 0.0


def connection():
    if not db.DB_PATH.exists():
        raise HTTPException(503, 'The Am I Broke? database is unavailable.')
    return db.connect(db.DB_PATH)


@router.get('')
def net_worth(currency: str = 'GBP', range: str = 'all', account: list[str] | None = Query(default=None)):
    def compute():
        conn = connection()
        try:
            conn.execute('BEGIN')
            return wealth.bundle(conn, path=db.DB_PATH, currency_code=currency, range_name=range, account_ids=account)
        finally:
            conn.close()
    try:
        # The workspace day can change before UTC midnight without a database
        # commit. Include it alongside the cache's existing UTC/revision key.
        with closing(connection()) as clock_conn:
            calendar_day = reporting_today(clock_conn).isoformat()
        return cached_json(db.DB_PATH, 'wealth', (currency, range, tuple(account) if account is not None else None, calendar_day), compute)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@router.post('/refresh')
def refresh_wealth():
    global _next_refresh
    if not _refresh_lock.acquire(blocking=False):
        raise HTTPException(429, 'Exchange rates are already refreshing.', headers={'Retry-After': '15'})
    conn = None
    try:
        remaining = _next_refresh - time.monotonic()
        if remaining > 0:
            raise HTTPException(429, 'Wait a minute before refreshing exchange rates again.', headers={'Retry-After': str(int(remaining) + 1)})
        _next_refresh = time.monotonic() + 60
        conn = connection()
        return wealth.refresh_fx(conn, path=db.DB_PATH)
    finally:
        if conn is not None:
            conn.close()
        _refresh_lock.release()


@router.get('/sources')
def source_links():
    """Inspect canonical membership; edits require explicit identity evidence."""
    conn = connection()
    try:
        accounts, _ = wealth.sources(conn, path=db.DB_PATH)
        links = source_reconciliation.list_links(conn, include_inactive=True)
        fields = ('id', 'name', 'institution', 'kind', 'source', 'canonical_id', 'source_ids',
                  'linked_to', 'source_status', 'default_included', 'exclusion_reason', 'drilldown', 'archived_by_user')
        return {'sources': [{key: account.get(key) for key in fields} for account in accounts],
                'links': [{key: row[key] for key in ('source_id', 'canonical_id', 'evidence_kind', 'created_at', 'updated_at', 'active')} for row in links],
                'archive_preferences': source_reconciliation.list_archive_preferences(conn),
                'methodology': 'Links are reversible projections backed by reviewed account identity. Original source records remain intact.'}
    finally:
        conn.close()


class SourceArchive(BaseModel):
    source_id: str = Field(min_length=1, max_length=300)
    archived: bool = Field(strict=True)


@router.post('/sources/archive')
def archive_source(body: SourceArchive):
    conn = connection()
    try:
        accounts, _ = wealth.sources(conn, path=db.DB_PATH)
        changed = source_reconciliation.set_archived(conn, source_id=body.source_id,
            archived=body.archived, accounts=accounts)
        return {'source_id': body.source_id, 'archived': body.archived, 'changed': changed}
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    finally:
        conn.close()


@router.post('')
def net_worth_selection(body: WealthAccountSelection, currency: str = 'GBP', range: str = 'all'):
    return net_worth(currency=currency, range=range, account=body.account)

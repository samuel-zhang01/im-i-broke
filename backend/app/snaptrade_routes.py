"""Same-origin read-only routes under Am I Broke?'s existing authentication middleware."""
import threading
import time

from fastapi import APIRouter, Path, Query
from fastapi.responses import JSONResponse

from .snaptrade_client import configured, fetch_portfolio
from . import snaptrade_store as store
from . import investment_activity

router = APIRouter(prefix='/api/investments', tags=['Connected investments'])
_lock = threading.Lock()
_next_read = 0.0
REFRESH_SECONDS = 60
HEADERS = {'Cache-Control': 'no-store', 'Pragma': 'no-cache'}
REFRESH_ERROR = 'The portfolio could not be retrieved. Please try again later.'


def portfolio_response(payload):
    # Runtime policy belongs on the response, not in an old saved snapshot.
    # Import locally because the scheduler uses this route's shared refresh lock.
    from .portfolio_scheduler import sync_seconds
    interval = sync_seconds()
    from .product_connections import has_saved_connections, sync_statuses
    saved_configured = has_saved_connections('snaptrade')
    if saved_configured:
        payload = {**payload, 'configured': True}
    payload = {**payload, 'connection_statuses': sync_statuses('snaptrade'), 'refresh_schedule': {
        'enabled': bool((configured() or saved_configured) and interval), 'interval_seconds': interval,
    }}
    return JSONResponse(payload, headers=HEADERS)


@router.get('/connected')
def connected_portfolio():
    cached = store.load_portfolio()
    if cached is not None:
        cached['configured'] = configured()
        return portfolio_response(cached)
    return refresh_portfolio()


@router.get('/connected/snapshots')
def portfolio_snapshots():
    return JSONResponse({'snapshots': store.list_snapshots()}, headers=HEADERS)


@router.get('/connected/activities/accounts')
def activity_accounts():
    return JSONResponse({'accounts': investment_activity.list_activity_accounts()}, headers=HEADERS)


@router.get('/connected/activities')
def saved_activities(account_id: str = Query(min_length=1, max_length=240),
                     limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0)):
    return JSONResponse(investment_activity.saved_activities(account_id, limit=limit, offset=offset),
                        headers=HEADERS)


@router.get('/connected/snapshots/{snapshot_id}')
def portfolio_snapshot(snapshot_id: int = Path(ge=1)):
    snapshot = store.get_snapshot(snapshot_id)
    if snapshot is None:
        return JSONResponse({'detail': 'This portfolio snapshot was not found.'}, 404, headers=HEADERS)
    return JSONResponse(snapshot, headers=HEADERS)


@router.post('/connected/refresh')
def refresh_portfolio():
    global _next_read
    from .product_connections import has_saved_connections, refresh_saved_connections
    saved_configured = has_saved_connections('snaptrade')
    # Saved credentials have independent leases and caches. A failed or cooling
    # down server connection must not prevent another household connection's read.
    if saved_configured:
        refresh_saved_connections('snaptrade')
    if not configured():
        if saved_configured:
            cached = store.load_portfolio()
            if cached is not None:
                return portfolio_response(cached)
            return JSONResponse({'detail': 'Saved connections could not be refreshed. Review their status in Settings.'}, 502, headers=HEADERS)
        cached = store.load_portfolio()
        if cached is not None:
            cached['configured'] = False
            cached['cache']['refresh_error'] = 'SnapTrade is not configured on the server. Saved data is still available.'
            return portfolio_response(cached)
        return portfolio_response({'configured': False, 'retrieved_at': store.now(),
                             'error': 'SnapTrade is not configured on the server yet.',
                             'accounts': [], 'connections': {'status': 'empty', 'data': [], 'error': None, 'truncated': False},
                             'cache': None})
    if not _lock.acquire(blocking=False):
        return JSONResponse({'detail': 'A portfolio refresh is already running. Please wait.'}, 429,
                            headers={**HEADERS, 'Retry-After': '15'})
    try:
        remaining = _next_read - time.monotonic()
        if remaining > 0:
            return JSONResponse({'detail': 'Please wait a minute between portfolio refreshes.'}, 429,
                                headers={**HEADERS, 'Retry-After': str(int(remaining) + 1)})
        _next_read = time.monotonic() + REFRESH_SECONDS
        try:
            result = fetch_portfolio()
            if result.get('error') and not result.get('accounts'):
                # A failed account-list read must never erase the last snapshot.
                cached = store.record_refresh_failure(result['error'])
                if cached is not None:
                    cached['configured'] = configured()
                    return portfolio_response(cached)
                return JSONResponse({'detail': result['error']}, 502, headers=HEADERS)
            result = store.save_portfolio(result)
            if saved_configured:
                result = store.load_portfolio()
                result['cache'].update(is_cached=False, environment_refresh_succeeded=True)
        except Exception:
            cached = store.record_refresh_failure(REFRESH_ERROR)
            if cached is not None:
                cached['configured'] = configured()
                return portfolio_response(cached)
            return JSONResponse({'detail': REFRESH_ERROR}, 502, headers=HEADERS)
        return portfolio_response(result)
    finally:
        _lock.release()

from fastapi import APIRouter, HTTPException, Query
from .account_selection import AccountSelection
from datetime import datetime, timezone

from .market_cache import BENCHMARKS, RefreshBusy, load_market_data, refresh_market_data
from . import db, investing_analytics as analysis
from .investment_read_cache import cached_json

router = APIRouter(prefix='/api/investments/market', tags=['Investment market data'])


@router.post('/refresh')
def refresh_market():
    try:
        result = refresh_market_data(include_benchmarks=True)
        from .intraday_market import refresh_intraday
        try:
            result['intraday'] = refresh_intraday()
        except RefreshBusy:
            result['intraday'] = {'status': 'busy', 'message': 'Hourly prices are already refreshing or were refreshed recently; the saved cache remains available.'}
        return result
    except RefreshBusy as error:
        raise HTTPException(429, str(error), headers={'Retry-After': '60'}) from None


@router.get('/value')
def market_value(currency: str = 'GBP', range: str = '1d', interval: str = '1h', account: list[str] | None = Query(default=None)):
    from .market_valuation import bundle
    def compute():
        if not db.DB_PATH.exists():
            raise HTTPException(503, 'The Am I Broke? database is unavailable.')
        conn = db.connect(db.DB_PATH)
        try:
            conn.execute('BEGIN')
            return bundle(conn, path=db.DB_PATH, currency_code=currency, range_name=range,
                          interval=interval, account_ids=account)
        finally:
            conn.close()
    try:
        return cached_json(db.DB_PATH, 'hourly-market-value', (currency, range, interval, tuple(account) if account is not None else None), compute)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@router.get('/benchmarks')
def benchmarks(range: str = '1m'):
    if range not in analysis.RANGES:
        raise HTTPException(422, 'Choose a supported date range.')
    return cached_json(db.DB_PATH, 'benchmarks', (range,), lambda: _benchmarks(range))


def _benchmarks(range):
    today = datetime.now(timezone.utc).date()
    cutoff = analysis._cutoff(range, today)
    cached = load_market_data(instrument_ids=[definition['instrument_id'] for definition in BENCHMARKS])
    series = []
    for definition in BENCHMARKS:
        raw = cached.get(definition['instrument_id'], {})
        points = analysis._points(raw, today)
        if cutoff:
            anchor = analysis._anchor(points, cutoff)
            points = ([anchor] if anchor else []) + [point for point in points if point['date'] > cutoff.isoformat()]
        first = points[0]['close'] if points else None
        series.append({'id': definition['instrument_id'], 'name': definition['name'],
            'currency': definition['currency'], 'as_of': raw.get('as_of'),
            'status': 'unavailable' if not points else ('stale' if raw.get('error') or analysis._old(points[-1]['date'], today, 5) else 'ready'),
            'points': [{**point, 'return_pct': analysis._percent(point['close'], first)} for point in points],
            'source_url': raw.get('source_url')})
    return {'range': range, 'series': series}




@router.post('/value')
def market_value_selection(body: AccountSelection, currency: str = 'GBP', range: str = '1d', interval: str = '1h'):
    return market_value(currency=currency, range=range, interval=interval, account=body.account)

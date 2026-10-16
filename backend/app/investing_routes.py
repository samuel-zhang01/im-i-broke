"""Private cached investing reads; shared authentication applies in main."""
from fastapi import APIRouter, HTTPException, Query
from .account_selection import AccountSelection
from . import db, investing_analytics, investing_calendar, investment_insights, investment_exposure
from .investment_read_cache import cached_json

router = APIRouter(prefix='/api/investments/analysis', tags=['Investing analysis'])


def connection():
    if not db.DB_PATH.exists():
        raise HTTPException(503, 'The Am I Broke? database is unavailable.')
    return db.connect(db.DB_PATH)


@router.get('')
def analysis(currency: str = 'GBP', range: str = '1m', account: list[str] | None = Query(default=None), paper: bool = False):
    return _read('analysis', (currency, range, tuple(account) if account is not None else None, paper),
                 lambda conn: investing_analytics.bundle(conn, path=db.DB_PATH, currency_code=currency,
                            range_name=range, account_ids=account, paper=paper))


def _read(scope, filters, produce):
    def compute():
        conn = connection()
        try:
            return produce(conn)
        finally:
            conn.close()
    try:
        return cached_json(db.DB_PATH, scope, filters, compute)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    except LookupError as error:
        raise HTTPException(404, str(error)) from None


@router.get('/security')
def security(account: str = Query(min_length=1, max_length=240),
             position: str = Query(min_length=1, max_length=500), currency: str = 'GBP', range: str = '1m'):
    return _read('security', (account, position, currency, range),
                 lambda conn: investing_analytics.security(conn, path=db.DB_PATH, account_id=account,
                            position_id=position, currency_code=currency, range_name=range))


@router.get('/calendar')
def calendar(currency: str = 'GBP', year: int | None = None, month: int | None = None,
             source: str = 'estimate', account: list[str] | None = Query(default=None), paper: bool = False):
    return _read('calendar', (currency, year, month, source, tuple(account) if account is not None else None, paper),
                 lambda conn: investing_calendar.bundle(conn, path=db.DB_PATH, currency_code=currency,
                            year=year, month=month, source=source, account_ids=account, paper=paper))


@router.get('/insights')
def insights(currency: str = 'GBP', account: list[str] | None = Query(default=None), paper: bool = False):
    return _read('insights', (currency, tuple(account) if account is not None else None, paper),
                 lambda conn: investment_insights.bundle(conn, path=db.DB_PATH, currency_code=currency,
                                                        account_ids=account, paper=paper))


@router.get('/exposure')
def exposure(currency: str = 'GBP', account: list[str] | None = Query(default=None), paper: bool = False):
    return _read('exposure', (currency, tuple(account) if account is not None else None, paper),
                 lambda conn: investment_exposure.bundle(conn, path=db.DB_PATH, currency_code=currency,
                                                         account_ids=account, paper=paper))


@router.get('/coverage')
def coverage(currency: str = 'GBP', account: list[str] | None = Query(default=None), paper: bool = False):
    from . import investment_coverage
    return _read('coverage', (currency, tuple(account) if account is not None else None, paper),
                 lambda conn: investment_coverage.bundle(conn, path=db.DB_PATH, currency_code=currency,
                                                         account_ids=account, paper=paper))


# Large explicit selections use a body to avoid proxy request-line limits.
# Delegate to the GET readers so cache keys, validation and read-only semantics
# remain identical; short filters stay in the query string.


@router.post('')
def analysis_selection(body: AccountSelection, currency: str = 'GBP', range: str = '1m', paper: bool = False):
    return analysis(currency=currency, range=range, account=body.account, paper=paper)


@router.post('/calendar')
def calendar_selection(body: AccountSelection, currency: str = 'GBP', year: int | None = None,
                       month: int | None = None, source: str = 'estimate', paper: bool = False):
    return calendar(currency=currency, year=year, month=month, source=source, account=body.account, paper=paper)


@router.post('/insights')
def insights_selection(body: AccountSelection, currency: str = 'GBP', paper: bool = False):
    return insights(currency=currency, account=body.account, paper=paper)


@router.post('/exposure')
def exposure_selection(body: AccountSelection, currency: str = 'GBP', paper: bool = False):
    return exposure(currency=currency, account=body.account, paper=paper)


@router.post('/coverage')
def coverage_selection(body: AccountSelection, currency: str = 'GBP', paper: bool = False):
    return coverage(currency=currency, account=body.account, paper=paper)

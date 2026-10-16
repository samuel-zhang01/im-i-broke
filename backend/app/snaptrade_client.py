"""Read-only SnapTrade Personal access with bounded, normalized responses."""
from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from functools import wraps
import logging
import os
import re
import time
from urllib.parse import urlsplit

from snaptrade_client import SnapTrade, SnapTradeAuth
from urllib3 import Timeout
from urllib3.exceptions import TimeoutError as TransportTimeoutError

from . import snaptrade_models as models

MAX_ACCOUNTS = 30
MAX_ROWS = 2000
ACTIVITY_PAGE_SIZE = 100
ACTIVITY_PAGES = 2
ACTIVITY_DAYS = 90
REQUEST_BUDGET_SECONDS = 70
ACCOUNT_UUID = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
READ_PATH = re.compile(r'(?:/api/v1)?/(?:accounts|authorizations|accounts/' + ACCOUNT_UUID
                       + r'|accounts/[a-zA-Z0-9-]+/(?:balances|positions/all|recentOrders|orders|activities|balanceHistory|returnRates))/?\Z')


class RequestBudgetExceeded(TimeoutError):
    """No new provider request was started because the refresh budget ended."""

# The SDK contains DEBUG statements with complete response bodies; turn off its
# loggers even if the host application's logging level changes during development.
for _name in ('snaptrade_client', 'snaptrade_client.rest', 'snaptrade_client.api_client', 'urllib3.connectionpool'):
    logging.getLogger(_name).disabled = True


def configured():
    return bool(os.environ.get('SNAPTRADE_ID', '').strip() and os.environ.get('SNAPTRADE_KEY', '').strip())


def restrict_transport(request):
    """Let the SDK sign requests, but deny mutations and cross-origin redirects."""
    @wraps(request)
    def readonly(method, url, *args, **kwargs):
        origin = urlsplit(url)
        if (method.upper() != 'GET' or origin.scheme != 'https'
                or origin.hostname != 'api.snaptrade.com' or origin.port not in (None, 443)
                or origin.username or origin.password or not READ_PATH.fullmatch(origin.path)):
            raise ValueError('SnapTrade operation is not permitted')
        # Live holdings and account totals can exceed the ordinary 12s read
        # deadline. Only those endpoints get more time. The 70s start budget
        # stops new requests; an in-flight call retains its socket timeout.
        path = origin.path.rstrip('/')
        current_data = (path.endswith('/positions/all')
                        or re.fullmatch(r'(?:/api/v1)?/accounts/' + ACCOUNT_UUID, path) is not None)
        kwargs.update(redirect=False, retries=False,
                      timeout=Timeout(connect=3, read=25 if current_data else 12,
                                      total=28 if current_data else 15))
        return request(method, url, *args, **kwargs)
    return readonly


def create_client(credentials=None):
    credentials = credentials or {'client_id': os.environ['SNAPTRADE_ID'].strip(),
                                   'consumer_key': os.environ['SNAPTRADE_KEY'].strip()}
    method = SnapTradeAuth.commercial_api_key if credentials.get('auth_method') == 'snaptrade_commercial' else SnapTradeAuth.personal_api_key
    client = SnapTrade(auth=method(
        client_id=credentials['client_id'], consumer_key=credentials['consumer_key']))
    pool = client.account_information.api_client.rest_client.pool_manager
    pool.request = restrict_transport(pool.request)
    return client


OPERATIONS = {
    'accounts': ('account_information', 'list_user_accounts'),
    'account_details': ('account_information', 'get_user_account_details'),
    'connections': ('connections', 'list_brokerage_authorizations'),
    'balances': ('account_information', 'get_user_account_balance'),
    'positions': ('account_information', 'get_all_account_positions'),
    'orders': ('account_information', 'get_user_account_recent_orders'),
    'historical_orders': ('account_information', 'get_user_account_orders'),
    'activities': ('account_information', 'get_account_activities'),
    'history': ('account_information', 'get_account_balance_history'),
    'return_rates': ('account_information', 'get_user_account_return_rates'),
}


def call(operation, *, credentials=None, **kwargs):
    group, name = OPERATIONS[operation]
    # Per-call client avoids shared mutable SDK signing state across threads.
    client = create_client(credentials) if credentials is not None else create_client()
    try:
        if credentials is not None and credentials.get('auth_method') == 'snaptrade_commercial':
            kwargs.update(user_id=credentials['user_id'], user_secret=credentials['user_secret'])
        return getattr(getattr(client, group), name)(**kwargs).body
    finally:
        client.account_information.api_client.rest_client.pool_manager.clear()


def safe_error(error):
    if isinstance(error, RequestBudgetExceeded):
        return 'The portfolio refresh time limit was reached before this request could start.'
    if isinstance(error, (TimeoutError, TransportTimeoutError)):
        return 'The request to SnapTrade timed out. Please try again later.'
    status = getattr(error, 'status', None)
    if status == 401:
        return 'SnapTrade rejected the credentials. Check the Personal API key on the server.'
    if status == 403:
        return 'SnapTrade did not allow access to this data. Check the connection in SnapTrade.'
    if status == 429:
        return 'SnapTrade is limiting requests. Wait at least a minute before refreshing.'
    if status in (404, 405, 501):
        return 'This brokerage does not currently provide this data through SnapTrade.'
    if isinstance(status, int) and 500 <= status <= 599:
        return 'SnapTrade returned a temporary server error. Please try again later.'
    if isinstance(error, (ValueError, TypeError, KeyError)):
        return 'SnapTrade returned an invalid or inconsistent response.'
    return 'This data could not be retrieved from SnapTrade. Please try again later.'


def fetch_historical_orders(account_id, *, days=30, read=call):
    """Bounded administrative evidence read, separate from the 24-hour UI feed.

    Orders may include canceled or partly filled intent. Their placement time
    is not an execution time, and this read neither imports transactions nor
    certifies historical cash completeness. Normal portfolio refreshes do not
    call this endpoint or add extra provider requests.
    """
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 90:
        raise ValueError('Choose an order lookback between 1 and 90 days.')
    try:
        body = read('historical_orders', account_id=account_id, days=days, state='all')
        items = models.rows(body)
        data = [{**models.order(item, index), 'executed_at': models.timestamp(item.get('time_executed'))}
                for index, item in enumerate(items[:MAX_ROWS])]
        return {**models.section(data, truncated=len(items) > MAX_ROWS), 'requested_days': days}
    except Exception as error:
        return {**models.section(error=safe_error(error),
                                 unsupported=getattr(error, 'status', None) in (404, 405, 501)),
                'requested_days': days}


def fetch_section(kind, account_id, read=call):
    data = []
    window = None
    try:
        if kind == 'activities':
            today = datetime.now(timezone.utc).date()
            start = today - timedelta(days=ACTIVITY_DAYS)
            window = {'requested_start': start.isoformat(), 'requested_end': today.isoformat(),
                      'pages_fetched': 0, 'pagination_complete': False}
            data, seen, truncated = [], set(), False
            duplicate_rows = False
            reported_totals = set()
            for page in range(ACTIVITY_PAGES):
                try:
                    body = read(kind, account_id=account_id, start_date=start,
                                end_date=today, offset=page * ACTIVITY_PAGE_SIZE, limit=ACTIVITY_PAGE_SIZE)
                except RequestBudgetExceeded:
                    if window['pages_fetched'] == 0:
                        window = None
                    raise
                window['pages_fetched'] += 1
                items = models.rows(body, 'data')
                for item in items[:ACTIVITY_PAGE_SIZE]:
                    row = models.activity(item, len(data))
                    if row['id'] not in seen:
                        data.append(row)
                        seen.add(row['id'])
                    else:
                        duplicate_rows = True
                if len(items) > ACTIVITY_PAGE_SIZE:
                    # Do not silently expand a bounded fetch if an upstream
                    # adapter ignores the requested page size.
                    truncated = True
                    break
                raw_total = models.obj(models.obj(body).get('pagination')).get('total')
                total = models.number(raw_total)
                if raw_total is not None and (total is None or total < 0 or not total.is_integer()):
                    raise ValueError('Invalid activity pagination total')
                if total is not None:
                    reported_totals.add(total)
                more = ((page + 1) * ACTIVITY_PAGE_SIZE < total) if total is not None else len(items) == ACTIVITY_PAGE_SIZE
                if not more:
                    # A short/overlapping response must not certify retrieval
                    # of a reported total that was never actually received.
                    window['pagination_complete'] = (not duplicate_rows and len(reported_totals) <= 1
                                                     and (total is None or len(data) == total))
                    truncated = not window['pagination_complete']
                    break
                truncated = page == ACTIVITY_PAGES - 1
            data.sort(key=lambda item: item['date'] or '', reverse=True)
            return {**models.section(data, truncated=truncated), **window}
        body = read(kind, account_id=account_id)
        if kind == 'history':
            return models.history(body)
        if kind == 'return_rates':
            return models.return_rates(body)
        if kind == 'balances':
            items = models.rows(body)
            data = [models.balance(item) for item in items[:MAX_ROWS]]
        elif kind == 'positions':
            items = models.rows(body, 'results')
            data = [models.position(item, i) for i, item in enumerate(items[:MAX_ROWS])]
        else:
            items = models.rows(body, 'orders')
            data = [models.order(item, i) for i, item in enumerate(items[:MAX_ROWS])]
            data.sort(key=lambda item: item['placed_at'] or '', reverse=True)
        result = models.section(data, truncated=len(items) > MAX_ROWS)
        if kind == 'positions':
            result['as_of'] = models.timestamp(models.obj(models.obj(body).get('data_freshness')).get('as_of'))
        return result
    except Exception as error:
        # Never return/log exception text: SDK exceptions can embed full bodies,
        # account identifiers and signed URLs. Independent sections remain usable.
        if kind == 'return_rates' and getattr(error, 'status', None) in (403, 404, 405, 501):
            # Optional brokerage capability: absence is a quiet local state,
            # not a portfolio failure or a request to enable a paid feature.
            return {**models.section(), 'status': 'unsupported'}
        if kind == 'history' and getattr(error, 'status', None) == 403:
            return models.section(error="Historical values are unavailable. SnapTrade's free history add-on may need to be enabled in the SnapTrade dashboard.",
                                  unsupported=True)
        return {**models.section(data if kind == 'activities' else [], error=safe_error(error),
                                 truncated=bool(data) and kind == 'activities',
                                 unsupported=getattr(error, 'status', None) in (404, 405, 501)),
                **(window or {})}


def distinct_accounts(raw_accounts, connections):
    """Collapse confirmed duplicate links without exposing institution IDs."""
    disabled = {row['id'] for row in connections if row['disabled'] is True}
    selected, duplicate_count = {}, 0
    for raw in raw_accounts:
        institution = models.text(raw.get('institution_name'))
        institution_id = models.text(raw.get('institution_account_id'), limit=1000)
        # Provider account identifiers can be reused in simulated namespaces.
        # Never let duplicate-link removal merge paper evidence with live money.
        identity = ('institution', institution, institution_id, raw.get('is_paper') is True) if institution and institution_id else ('snaptrade', raw.get('id'))
        holdings = models.obj(models.obj(raw.get('sync_status')).get('holdings'))
        priority = (raw.get('brokerage_authorization') not in disabled,
                    holdings.get('holdings_unavailable') is not True,
                    bool(models.obj(models.obj(raw.get('balance')).get('total'))))
        if identity in selected:
            duplicate_count += 1
            if priority > selected[identity][0]:
                selected[identity] = (priority, raw)
        else:
            selected[identity] = (priority, raw)
    return [raw for _, raw in selected.values()], duplicate_count


def fetch_account_value(account, read=call):
    """Read the account-detail total without inventing NAV freshness.

    /accounts is always daily cached. /accounts/{id} uses the API key's
    real-time or daily access and returns a brokerage total, including cash.
    Its Account schema has no NAV observation timestamp: sync_status remains
    the daily holdings sync even when this total was obtained in real time.
    Do not use that clock, position freshness, or HTTP Date as NAV freshness.

    Missing totals are unknown, not zero. Malformed or mismatched responses
    raise so the portfolio refresh cannot downgrade a newer saved NAV to the
    old discovery total. The route retains the last snapshot on this failure.
    """
    detail = models.obj(read('account_details', account_id=account['id']))
    if (detail.get('id') != account['id']
            or models.text(detail.get('brokerage_authorization')) != account['connection_id']
            or models.boolean(detail.get('is_paper')) is not account['is_paper']):
        raise ValueError('Account detail does not match the discovered account')
    balance = detail.get('balance')
    if not isinstance(balance, Mapping) or 'total' not in balance:
        raise ValueError('Account detail has no valid balance object')
    total = balance['total']
    if total is None:
        return {'total_value': None, 'currency': None, 'total_value_as_of': None}
    if not isinstance(total, Mapping) or 'amount' not in total:
        raise ValueError('Account detail has no valid total object')
    value, currency = models.number(total.get('amount')), models.currency(total.get('currency'))
    if (value is None and total['amount'] is not None) or currency is None:
        raise ValueError('Account detail has no valid total amount and currency')
    return {'total_value': value, 'currency': currency, 'total_value_as_of': None}


def fetch_portfolio(read=call, *, credentials_configured=None):
    result = {'configured': configured() if credentials_configured is None else bool(credentials_configured), 'retrieved_at': datetime.now(timezone.utc).isoformat(),
              'error': None, 'accounts': [], 'connections': models.section()}
    if not result['configured']:
        result['error'] = 'SnapTrade is not configured on the server yet.'
        return result
    deadline = time.monotonic() + REQUEST_BUDGET_SECONDS
    def bounded_read(*args, **kwargs):
        # Stop starting new work after the budget. An already-running request
        # still has its socket timeout; this is not a hard wall-clock timeout.
        if time.monotonic() >= deadline:
            raise RequestBudgetExceeded('Portfolio refresh budget exhausted')
        return read(*args, **kwargs)
    # Concurrency is bounded across all accounts and sections, not per account.
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix='portfolio-read') as pool:
        roots = {pool.submit(bounded_read, name): name for name in ('accounts', 'connections')}
        raw_accounts = []
        for future in as_completed(roots):
            kind = roots[future]
            try:
                items = models.rows(future.result())
                if kind == 'accounts':
                    raw_accounts = items
                else:
                    result['connections'] = models.section([models.connection(item) for item in items])
            except Exception as error:
                if kind == 'accounts':
                    result['error'] = safe_error(error)
                else:
                    result['connections'] = models.section(error=safe_error(error))
        raw_accounts, duplicate_count = distinct_accounts(raw_accounts, result['connections']['data'])
        warnings = []
        if duplicate_count:
            warnings.append(f'{duplicate_count} duplicate account connection(s) excluded to avoid counting the same account twice.')
        if len(raw_accounts) > MAX_ACCOUNTS:
            warnings.append(f'Showing the first {MAX_ACCOUNTS} accounts; some accounts are omitted.')
        if warnings:
            result['error'] = ' '.join(warnings)
        accounts, seen = [], set()
        institutions = {connection['id']: connection['institution'] for connection in result['connections']['data']}
        for raw in raw_accounts[:MAX_ACCOUNTS]:
            item = models.account(raw)
            if item['institution'] == 'Brokerage':
                item['institution'] = institutions.get(item['connection_id']) or 'Brokerage'
            if not item['id'] or item['id'] in seen:
                continue
            seen.add(item['id'])
            accounts.append(item)
        # Details share the same four-worker pool and overall request budget.
        # Complete them before independent sections: one failed required total
        # aborts the refresh, preserving the previous cache atomically instead
        # of replacing a newer NAV with the daily discovery endpoint's value.
        detail_futures = {pool.submit(fetch_account_value, account, bounded_read): account
                          for account in accounts
                          if not (account['holdings_unavailable'] and account['total_value'] is None)}
        for future in as_completed(detail_futures):
            try:
                detail_futures[future].update(future.result())
            except Exception as error:
                for pending in detail_futures:
                    pending.cancel()
                result['error'] = 'Current account values could not be retrieved. ' + safe_error(error)
                result['retrieved_at'] = datetime.now(timezone.utc).isoformat()
                return result
        result['accounts'] = accounts
        # Fetch current quantities and cash across every account before optional
        # feeds can consume the remaining budget. The same four workers and
        # deadline apply to both phases; failed sections remain independently
        # visible and retain their previous data at the persistence boundary.
        for kinds in (('positions', 'balances'), ('orders', 'activities', 'history', 'return_rates')):
            futures = {}
            for kind in kinds:
                for account in accounts:
                    if kind in {'balances', 'positions'} and account['holdings_unavailable']:
                        account[kind] = models.section(error='The brokerage does not expose holdings for this account.', unsupported=True)
                    else:
                        futures[pool.submit(fetch_section, kind, account['id'], bounded_read)] = (account, kind)
            for future in as_completed(futures):
                account, kind = futures[future]
                account[kind] = future.result()
                if kind == 'positions' and account[kind].get('as_of'):
                    account['holdings_synced_at'] = account[kind]['as_of']
    result['retrieved_at'] = datetime.now(timezone.utc).isoformat()
    return result

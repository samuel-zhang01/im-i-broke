"""Bounded, read-only Lunch Flow Personal API transport.

Credentials are server environment only. Neither provider bodies nor exception
strings are logged or returned as errors, and redirects can never carry the key.
"""
from __future__ import annotations

import json
import hashlib
from collections import Counter
import math
import os
import re
import time
from datetime import date
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ORIGIN = 'https://www.lunchflow.app/api/v1'
MAX_BYTES = 16 * 1024 * 1024
MAX_ACCOUNTS = 100
MAX_TRANSACTIONS = 50000
TIMEOUT_SECONDS = 20
REQUEST_BUDGET_SECONDS = 240


class ProviderError(ValueError):
    """Messages are safe for the application UI."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def configured():
    return bool(os.environ.get('LUNCH_FLOW_API', '').strip())


def identifier(value):
    if isinstance(value, bool) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', str(value or '')):
        raise ProviderError('Lunch Flow returned an invalid account or transaction identifier.')
    return str(value)


def currency(value):
    value = str(value or '').upper()
    return value if re.fullmatch('[A-Z]{3}', value) else None


def number(value):
    if isinstance(value, bool):
        raise ProviderError('Lunch Flow returned an invalid amount.')
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ProviderError('Lunch Flow returned an invalid amount.') from None
    if not math.isfinite(result) or abs(result) > 1e14:
        raise ProviderError('Lunch Flow returned an invalid amount.')
    return result


def text(value, limit=2000):
    return str(value or '')[:limit]


GENERIC_MERCHANTS = {
    'payment','transfer','card payment','debit card','credit card','direct debit',
    'transaction','unknown','n a','na','none','null','not provided','not available',
    'cash','purchase','pos','pos purchase','card transaction','bank transfer',
}


def merchant_label_quality(value):
    """Distinguish populated merchant names from references and placeholders."""
    label = text(value,500).strip()
    if not label:
        return 'missing'
    words = re.sub(r'[^\w]+',' ',label.casefold()).strip()
    if words in GENERIC_MERCHANTS:
        return 'generic'
    if not any(character.isalpha() for character in label):
        return 'reference'
    compact = re.sub(r'[\s-]','',label)
    letters = sum(character.isalpha() for character in compact)
    digits = sum(character.isdigit() for character in compact)
    if (re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',label,re.I)
            or (len(compact) >= 12 and digits >= 8 and letters <= 5)):
        return 'reference'
    return 'usable'


def merchant_label(merchant, description):
    """Display evidence already supplied by the bank; never infer a business.

    The caller retains raw provider fields. Description fallback is a local
    label, not a verified merchant identity or a description of items bought.
    """
    quality = merchant_label_quality(merchant)
    if quality == 'usable':
        return {'merchant':text(merchant,500),'merchant_source':'provider','merchant_quality':quality}
    description = text(description).strip()
    if any(character.isalpha() for character in description):
        from .categorize import normalize_merchant
        # The legacy normalizer is Latin-only; preserve non-Latin bank text.
        label = normalize_merchant(description) if re.search(r'[A-Za-z]',description) else description
        return {'merchant':label,'merchant_source':'description','merchant_quality':quality}
    return {'merchant':'Bank transaction','merchant_source':'unavailable','merchant_quality':quality}


def safe_error(error):
    return str(error) if isinstance(error, ProviderError) else 'Lunch Flow could not be read. Your saved data is unchanged.'


def read(resource, account_id=None, *, date_from=None, date_to=None, api_key=None):
    key = api_key.strip() if isinstance(api_key, str) else os.environ.get('LUNCH_FLOW_API', '').strip()
    if not key:
        raise ProviderError('Lunch Flow is not configured on the server.')
    if resource == 'accounts' and account_id is None:
        path = '/accounts'
    elif resource in {'transactions', 'balance'} and account_id is not None:
        path = '/accounts/' + identifier(account_id) + '/' + resource
    else:
        raise ProviderError('This Lunch Flow operation is not permitted.')
    params = {}
    if resource == 'transactions':
        params['include_pending'] = 'true'
        for field, value in (('from', date_from), ('to', date_to)):
            if value is not None:
                try:
                    params[field] = date.fromisoformat(str(value)).isoformat()
                except ValueError:
                    raise ProviderError('Transaction dates must use YYYY-MM-DD.') from None
    url = ORIGIN + path + ('?' + urlencode(params) if params else '')
    request = Request(url, headers={'x-api-key': key,
                                   'Accept': 'application/json', 'User-Agent': 'Ledger/1.0'}, method='GET')
    try:
        with build_opener(ProxyHandler({}), NoRedirects()).open(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ProviderError('Lunch Flow response exceeded the safe size limit; saved data is unchanged.')
            data = json.loads(body)
    except HTTPError as error:
        code = error.code
        error.close()
        messages = {401: 'Lunch Flow rejected the API key. Check the server configuration.',
                    403: 'Lunch Flow denied access. Check the account connection in Lunch Flow.',
                    429: 'Lunch Flow is limiting requests. Please wait before syncing again.'}
        raise ProviderError(messages.get(code, 'Lunch Flow is temporarily unavailable. Your saved data is unchanged.')) from None
    except (URLError, OSError, TimeoutError):
        raise ProviderError('Lunch Flow could not be reached securely. Your saved data is unchanged.') from None
    except (ValueError, UnicodeError) as error:
        if isinstance(error, ProviderError):
            raise
        raise ProviderError('Lunch Flow returned an unreadable response. Your saved data is unchanged.') from None
    if not isinstance(data, dict):
        raise ProviderError('Lunch Flow returned an invalid response.')
    return data


def list_accounts(data):
    items = data.get('accounts')
    if not isinstance(items, list) or len(items) > MAX_ACCOUNTS:
        raise ProviderError('Lunch Flow returned an invalid or oversized account list.')
    result, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ProviderError('Lunch Flow returned an invalid account.')
        key = identifier(item.get('id'))
        if key in seen:
            raise ProviderError('Lunch Flow returned duplicate account identifiers.')
        seen.add(key)
        result.append({'id': key, 'name': text(item.get('name'), 300),
                       'institution_name': text(item.get('institution_name'), 200) or 'Bank',
                       'provider': text(item.get('provider'), 100),
                       'currency': currency(item.get('currency')), 'status': text(item.get('status'), 80),
                       'raw': item})
    if isinstance(data.get('total'), int) and data['total'] > len(items):
        raise ProviderError('Lunch Flow returned an incomplete account list; saved data is unchanged.')
    return result


def transactions(data, account_id):
    items = data.get('transactions')
    if not isinstance(items, list) or len(items) > MAX_TRANSACTIONS:
        raise ProviderError('Lunch Flow returned an invalid or oversized transaction list.')
    result, seen, pending_occurrences = [], set(), Counter()
    for item in items:
        if not isinstance(item, dict):
            raise ProviderError('Lunch Flow returned an invalid transaction.')
        raw_id = item.get('id')
        if isinstance(raw_id,bool) or (raw_id is not None and not isinstance(raw_id,(str,int))):
            raise ProviderError('Lunch Flow returned an invalid transaction identifier.')
        key = str(raw_id) if raw_id is not None else ''
        if not key and item.get('isPending') is True:
            fingerprint = hashlib.sha256(json.dumps(item,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            occurrence = pending_occurrences[fingerprint]
            pending_occurrences[fingerprint] += 1
            key = f'pending:{fingerprint}:{occurrence}'
        if not key or len(key) > 500 or any(ord(c) < 32 for c in key):
            raise ProviderError('Lunch Flow returned an invalid transaction identifier.')
        if key in seen:
            raise ProviderError('Lunch Flow returned duplicate transaction identifiers.')
        seen.add(key)
        if item.get('accountId') is not None and identifier(item['accountId']) != str(account_id):
            raise ProviderError('Lunch Flow returned a transaction for a different account.')
        try:
            day = date.fromisoformat(str(item.get('date') or '')[:10]).isoformat()
        except ValueError:
            raise ProviderError('Lunch Flow returned an invalid transaction date.') from None
        native = currency(item.get('currency'))
        if native is None or not isinstance(item.get('isPending', False), bool):
            raise ProviderError('Lunch Flow returned an invalid transaction currency or status.')
        result.append({'provider_id': key, 'date': day, 'amount': number(item.get('amount')), 'currency': native,
                       'merchant': text(item.get('merchant'), 500), 'description': text(item.get('description')),
                       'is_pending': bool(item.get('isPending', False)), 'raw': item})
    if isinstance(data.get('total'), int) and data['total'] > len(items):
        raise ProviderError('Lunch Flow returned incomplete transactions; the previous cache is preserved.')
    return result


def balance(data):
    item = data.get('balance')
    if not isinstance(item, dict) or currency(item.get('currency')) is None:
        raise ProviderError('Lunch Flow did not return a usable account balance.')
    return {'amount': number(item.get('amount')), 'currency': currency(item['currency'])}


def fetch_account(account_id, reader=read, deadline=None):
    result = {'balance': None, 'transactions': None, 'errors': []}
    for operation, parser in (('balance', balance), ('transactions', lambda data: transactions(data, account_id))):
        try:
            if deadline is not None and time.monotonic() >= deadline:
                raise ProviderError('The sync time budget was reached; remaining accounts will retry later.')
            result[operation] = parser(reader(operation, account_id))
        except Exception as error:
            result['errors'].append(safe_error(error))
    return result

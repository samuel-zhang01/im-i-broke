"""Optional, user-triggered public-business lookup; results never edit the ledger.

Only saved business names can leave the app. Server requests require an operator
contact for Wikimedia's User-Agent policy; browser requests can use the user's
normal browser identity and submit their untrusted candidates for local caching.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import os
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request
from .network import urlopen

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .db import DB_PATH, connect
from .merchants import key

router = APIRouter()
API_URL = 'https://www.wikidata.org/w/api.php'
CACHE_DAYS = 30
MAX_RESPONSE_BYTES = 500_000
_request_lock = threading.Lock()
_last_request = 0.0
_retry_until = 0.0

_HUMAN_DESCRIPTION = re.compile(
    r'\b(human|person|actor|actress|singer|politician|footballer|businessman|businesswoman|'
    r'businessperson|entrepreneur|founder|executive|scientist|writer|author|surname|given name|'
    r'disambiguation|Wikimedia category)\b', re.I)
_CATEGORIES = [
    (r'\b(restaurant|caf[eé]|coffeehouse|coffee shop|pub|bar|pizzeria|fast.food)\b', 'Eating out'),
    (r'\b(supermarket|grocery|groceries)\b', 'Groceries'),
    (r'\b(broadband|telecommunications|internet service|mobile network|esim)\b', 'Phone & internet'),
    (r'\b(utility company|water supply|electric utility|district heating)\b', 'Bills & utilities'),
    (r'\b(travel agency|online travel|tour operator|hotel chain|airline|booking website)\b', 'Travel'),
    (r'\b(railway company|rail operator|public transport)\b', 'Transport'),
    (r'\b(dental practice|dental clinic|healthcare|pharmacy|fitness cent[er]{2})\b', 'Health & education'),
    (r'\b(video game|gaming platform|ticket|ticketing|entertainment|cinema|theatre)\b', 'Entertainment'),
    (r'\b(streaming service|streaming platform|software company|software service)\b', 'Subscriptions'),
    (r'\b(retailer|retail|clothing|fashion|brand|ecommerce|e.commerce|online marketplace)\b', 'Shopping'),
]
_BUSINESS_DESCRIPTION = re.compile(
    r'\b(company|business|chain|manufacturer|store|platform|service|marketplace|laundry|dry.cleaning)\b', re.I)


class LookupFailure(Exception):
    def __init__(self, status='unavailable', message='Wikidata is unavailable. Try again later.', retry_after=None):
        self.status, self.message, self.retry_after = status, message, retry_after


class LookupCandidate(BaseModel):
    id: str = Field(max_length=20)
    label: str = Field(max_length=160)
    description: str = Field(default='', max_length=500)
    instance_of: list[str] = Field(default_factory=list, max_length=30)


class CacheLookup(BaseModel):
    query: str = Field(min_length=2, max_length=80)
    matches: list[LookupCandidate] = Field(max_length=5)


def _approved_query(conn, query):
    query = re.sub(r'\s+', ' ', query).strip()
    if (not 2 <= len(query) <= 80 or not any(c.isalpha() for c in query)
            or any(not (c.isalnum() or c in " '&.-") for c in query)
            or re.search(r'\d{4,}|\b(?:transfer|payment|account|iban|sort code)\b', query, re.I)):
        raise ValueError('Use a saved public business name, without statement text or account references.')
    needle = key(query)
    # Personal aliases stay private even if a duplicate business record exists.
    private = conn.execute("SELECT canonical_name AS name FROM merchants WHERE kind IN ('person','self') "
                           "UNION SELECT a.pattern FROM merchant_aliases a JOIN merchants m "
                           "ON m.id=a.merchant_id WHERE m.kind IN ('person','self')")
    if any(key(row['name']) == needle for row in private):
        raise ValueError('Personal and own-account names cannot be looked up.')
    matches = [row for row in conn.execute("SELECT canonical_name FROM merchants WHERE kind='merchant' AND enabled=1")
               if key(row['canonical_name']) == needle]
    if not matches:
        raise ValueError('Save this name as a business before looking it up. Personal and unclassified names stay local.')
    canonical = matches[0]['canonical_name']
    # Revalidate the stored spelling too; lookup never substitutes a raw alias.
    if any(not (c.isalnum() or c in " '&.-") for c in canonical) or re.search(r'\d{4,}', canonical):
        raise ValueError('The saved business name contains a reference or unsupported characters; edit its name first.')
    return canonical


def normalize_matches(candidates):
    """Discard humans/non-business results and construct safe public source links."""
    matches, seen = [], set()
    for candidate in candidates[:5]:
        if not isinstance(candidate, dict):
            continue
        ident = candidate.get('id', '')
        label = candidate.get('label', '')
        description = candidate.get('description', '')
        if not all(isinstance(v, str) for v in (ident, label, description)):
            continue
        if not re.fullmatch(r'Q[1-9]\d{0,14}', ident) or ident in seen:
            continue
        if (not label.strip() or len(label) > 160 or len(description) > 500
                or any(c in label + description for c in '<>\r\n')):
            continue
        if 'Q5' in (candidate.get('instance_of') or []) or _HUMAN_DESCRIPTION.search(description):
            continue
        category = next((cat for pattern, cat in _CATEGORIES if re.search(pattern, description, re.I)), None)
        if not category and not _BUSINESS_DESCRIPTION.search(description):
            continue
        seen.add(ident)
        matches.append({'id': ident, 'label': label.strip(), 'description': description.strip(),
                        'source_url': f'https://www.wikidata.org/wiki/{ident}',
                        'suggested_category': category})
    return matches


def _retry_seconds(header):
    try:
        return max(5, min(86400, int(header)))
    except (ValueError, TypeError):
        try:
            return max(5, min(86400, int((parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds())))
        except (ValueError, TypeError, OverflowError):
            return 60


def _request_json(parameters, user_agent):
    global _last_request, _retry_until
    with _request_lock:
        now = time.monotonic()
        if now < _retry_until:
            raise LookupFailure('rate_limited', 'Wikidata asked this app to wait before retrying.', int(_retry_until-now)+1)
        time.sleep(max(0, 1.0 - (now - _last_request)))
        request = Request(API_URL + '?' + urlencode({**parameters, 'format': 'json', 'maxlag': 5}),
                          headers={'User-Agent': user_agent, 'Accept': 'application/json'})
        try:
            with urlopen(request, timeout=6) as response:
                data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise LookupFailure(message='Wikidata returned an oversized response.')
            payload = json.loads(data)
            if not isinstance(payload, dict):
                raise LookupFailure(message='Wikidata returned an unexpected response.')
            if 'error' in payload:
                if payload['error'].get('code') in ('maxlag', 'ratelimited'):
                    _retry_until = time.monotonic() + 60
                    raise LookupFailure('rate_limited', 'Wikidata is busy. Try again in a minute.', 60)
                raise LookupFailure(message='Wikidata could not complete this lookup.')
            return payload
        except HTTPError as error:
            retry_header = error.headers.get('Retry-After')
            error.close()
            if error.code in (429, 503):
                retry = _retry_seconds(retry_header)
                _retry_until = time.monotonic() + retry
                raise LookupFailure('rate_limited', 'Wikidata asked this app to wait before retrying.', retry) from error
            raise LookupFailure() from error
        except (URLError, TimeoutError, ValueError) as error:
            raise LookupFailure() from error
        finally:
            _last_request = time.monotonic()


def _fetch_matches(query, contact):
    agent = f'OceanDepthsMerchantLookup/1.0 ({contact})'
    search = _request_json({'action': 'wbsearchentities', 'search': query,
                            'language': 'en', 'type': 'item', 'limit': 5}, agent)
    if not isinstance(search.get('search'), list):
        raise LookupFailure(message='Wikidata returned an unexpected search response.')
    candidates = [r for r in search['search'][:5] if isinstance(r, dict) and re.fullmatch(r'Q[1-9]\d{0,14}', str(r.get('id', '')))]
    if not candidates:
        return []
    details = _request_json({'action': 'wbgetentities', 'ids': '|'.join(r['id'] for r in candidates),
                             'props': 'claims|labels|descriptions', 'languages': 'en'}, agent)
    if not isinstance(details.get('entities'), dict):
        raise LookupFailure(message='Wikidata returned an unexpected entity response.')
    checked = []
    for row in candidates:
        entity = details['entities'].get(row['id'], {})
        if not isinstance(entity, dict) or 'missing' in entity:
            continue
        types = [claim.get('mainsnak', {}).get('datavalue', {}).get('value', {}).get('id')
                 for claim in entity.get('claims', {}).get('P31', [])]
        checked.append({'id': row['id'], 'label': entity.get('labels', {}).get('en', {}).get('value', row.get('label', '')),
                        'description': entity.get('descriptions', {}).get('en', {}).get('value', row.get('description', '')),
                        'instance_of': types})
    return normalize_matches(checked)


def _cached(conn, query):
    row = conn.execute('SELECT payload,fetched_at FROM enrichment_cache WHERE query_key=?', (key(query),)).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row['payload'])
        fetched = datetime.fromisoformat(row['fetched_at'])
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        if not isinstance(payload.get('matches'), list):
            return None
        # Recheck display fields even if a local cache row is malformed.
        payload['matches'] = normalize_matches(payload['matches'])
        return payload, row['fetched_at'], datetime.now(timezone.utc)-fetched > timedelta(days=CACHE_DAYS)
    except (ValueError, TypeError, AttributeError):
        return None


def _response(query, matches=None, *, status=None, cached=False, stale=False, fetched_at=None, retry_after=None, message=None):
    matches = matches or []
    return {'query': query, 'provider': 'Wikidata', 'status': status or ('ok' if matches else 'empty'),
            'cached': cached, 'stale': stale, 'fetched_at': fetched_at, 'retry_after': retry_after,
            'search_url': 'https://www.wikidata.org/w/index.php?' + urlencode({'title': 'Special:Search', 'search': query, 'ns0': 1}),
            'message': message or ('Public business suggestions only. Review before changing a merchant.' if matches
                                   else 'No clear business match found. Wikidata does not cover every merchant.'),
            'matches': matches}


def cache_matches(conn, query, candidates):
    query = _approved_query(conn, query)
    matches = normalize_matches(candidates)
    fetched_at = datetime.now(timezone.utc).isoformat()
    conn.execute('INSERT INTO enrichment_cache(query_key,payload,fetched_at) VALUES(?,?,?) '
                 'ON CONFLICT(query_key) DO UPDATE SET payload=excluded.payload,fetched_at=excluded.fetched_at',
                 (key(query), json.dumps({'matches': matches}), fetched_at))
    return _response(query, matches, fetched_at=fetched_at)


def lookup(conn, query):
    query = _approved_query(conn, query)
    stored = _cached(conn, query)
    if stored and not stored[2]:
        return _response(query, stored[0]['matches'], cached=True, fetched_at=stored[1])
    previous = {'matches': stored[0]['matches'], 'cached': True, 'stale': True, 'fetched_at': stored[1]} if stored else {}
    contact = os.environ.get('FINANCE_WIKIDATA_CONTACT', '').strip()
    if (not contact or len(contact) > 250 or '\n' in contact or '\r' in contact
            or not re.search(r'https?://[^\s]+|[^\s@]+@[^\s@]+\.[^\s@]+|\bUser:[^\s]+', contact)):
        return _response(query, status='browser_lookup_required', **previous,
                         message='Look up this saved business directly from your browser, then cache the suggestions locally.')
    try:
        return cache_matches(conn, query, _fetch_matches(query, contact))
    except LookupFailure as error:
        return _response(query, status=error.status, message=error.message,
                         retry_after=error.retry_after, **previous)
    except (ValueError, TypeError, KeyError, AttributeError):
        return _response(query, status='unavailable', **previous,
                         message='Wikidata returned an unexpected response. Cached suggestions are retained.')


@router.get('/api/merchant-lookup')
def merchant_lookup(query: str = Query(min_length=2, max_length=80)):
    conn = connect(DB_PATH)
    try:
        try:
            result = lookup(conn, query)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        conn.commit()
        return result
    finally:
        conn.close()


@router.post('/api/merchant-lookup/cache')
def merchant_lookup_cache(request: CacheLookup):
    conn = connect(DB_PATH)
    try:
        try:
            result = cache_matches(conn, request.query, [r.model_dump() for r in request.matches])
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        conn.commit()
        return result
    finally:
        conn.close()

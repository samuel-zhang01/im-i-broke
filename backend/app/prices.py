"""Public indicative quotes and dated reference FX. See docs/reference/FINANCIAL_MODEL.md.

Only public securities, currencies and dates leave this module. Failed or invalid
responses never create zero prices or turn old exchange times into current ones.
"""
from __future__ import annotations
import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Optional

_UA = {'User-Agent': 'LedgerPersonalFinance/1.0 (local portfolio market-data client)', 'Accept': 'application/json'}
_SEARCH = 'https://query1.finance.yahoo.com/v1/finance/search?'
_CHART = 'https://query1.finance.yahoo.com/v8/finance/chart/'
_REFERENCE = 'https://api.frankfurter.dev/v2/rates?'
_FX_TTL = 15 * 60
_fx_cache: dict[str, tuple[float, Optional[dict]]] = {}
_fx_lock = threading.Lock()
_cooldowns: dict[str, float] = {}
_local = threading.local()

# Public exceptions where ISIN search misses the security or returns another
# currency listing. London suffix alone does NOT imply pence: VUAG uses GBP.
_LISTINGS = {
 'IE00BK5BQW10': ('https://www.vanguard.co.uk/uk-fund-directory/product/etf/equity/9680/ftse-north-america-ucits-etf-usd-accumulating', 'VNRG', {'VNRG': [('VNRG.L', 'GBP', 'LSE')]}),
 'IE00BK5BQX27': ('https://fund-docs.vanguard.com/FTSE_Developed_Europe_UCITS_ETF_EUR_Accumulating_9681_EU_INT_UK_EN.pdf', 'VEUA', {'VEUA': [('VEUA.L', 'GBP', 'LSE')]}),
 'IE00BK5BR733': ('https://www.vanguard.co.uk/uk-fund-directory/product/etf/equity/9678/ftse-emerging-markets-ucits', 'VFEG', {'VFEG': [('VFEG.L', 'GBP', 'LSE')]}),
 'IE00BFMXYX26': ('https://fund-docs.vanguard.com/FTSE_Japan_UCITS_ETF_USD_Accumulating_9674_EU_INT_UK_EN.pdf', 'VJPB', {'VJPB': [('VJPB.L', 'GBP', 'LSE')]}),
 'IE00BK5BQZ41': ('https://www.vanguard.co.uk/uk-fund-directory/product/etf/equity/9676/ftse-developed-asia-pacific-ex-japan-ucits-etf-usd-accumulating', 'VDPG', {'VDPG': [('VDPG.L', 'GBP', 'LSE')]}),
 'US02079K3059': ('https://www.six-group.com/en/market-data/shares/share-explorer/share-details.US02079K3059USD4.html', 'GOOGL', {'GOOGL': [('GOOGL', 'USD', 'NMS')]}),
 'IE00B53HP851': ('https://www.blackrock.com/uk/intermediaries/products/253716/_1_1', 'CUKX', {'CUKX': [('CUKX.L', 'GBp', 'LSE')]}),
 'IE00BFMXXD54': ('https://www.vanguard.co.uk/professional/product/etf/equity/9694/vanguard-sp-500-ucits-etf-usd-accumulating', 'VUAG', {'VUAG': [('VUAG.L', 'GBP', 'LSE')], 'VUAA': [('VUAA.L', 'USD', 'LSE')]}),
 'XS2399364582': ('https://leverageshares.com/documents/pdfs/en/Leverage-Shares-Products.pdf', '5SPY', {'5SPY': [('5SPY.L', 'GBp', 'LSE')], 'SP5Y': [('SP5Y.L', 'USD', 'LSE')]}),
 'XS2337090422': ('https://leverageshares.com/documents/factsheet/3x_amd_factsheet.pdf', 'AMD3', {'AMD3': [('AMD3.L', 'USD', 'LSE')], '3AMD': [('3AMD.L', 'GBp', 'LSE')]}),
 'US8740391003': ('https://investor.tsmc.com/static/dividendHistory/Dividend_Announcement_2019.pdf', 'TSM', {'TSM': [('TSM', 'USD', 'NYQ')]}),
}
for _isin, _ticker, _source in (
 ('CA0636711016', 'BMO', 'https://newsroom.bmo.com/2026-09-02-Bank-of-Montreal-Receives-Regulatory-Approvals-for-Normal-Course-Issuer-Bid'),
 ('CA0641491075', 'BNS', 'https://www.scotiabank.com/ca/en/about/investors-shareholders/frequently-asked-questions.html'),
 ('CA1363851017', 'CNQ', 'https://www.cnrl.com/investors/stock-information/'),
 ('CA7800871021', 'RY', 'https://www.rbc.com/investor-relations/share-information.html'),
 ('CA8911605092', 'TD', 'https://www.td.com/ca/en/about-td/for-investors/investor-relations/share-information/common-shares'),
):
 _LISTINGS[_isin] = (_source, _ticker, {_ticker: [(_ticker, 'USD', 'NYQ'), (_ticker + '.TO', 'CAD', 'TOR')]})


def _failure(code):
    _local.error = code
    return None


from .network import urlopen


def _get_json(url: str, timeout: float = 8.0):
    host = urllib.parse.urlsplit(url).hostname
    if _cooldowns.get(host, 0) > time.monotonic():
        return _failure('provider_rate_limited')
    try:
        with urlopen(urllib.request.Request(url, headers=_UA), timeout=timeout) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            return _failure('provider_response_too_large')
        return json.loads(raw)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            retry = exc.headers.get('Retry-After', '60') if exc.headers else '60'
            _cooldowns[host] = time.monotonic() + (min(3600, max(60, int(retry))) if retry.isdigit() else 60)
        exc.close()
        return _failure('provider_rate_limited' if exc.code == 429 else 'provider_http_error')
    except (OSError, ValueError, TimeoutError):
        return _failure('provider_unavailable')


def _positive(value):
    try:
        return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value > 0
    except OverflowError:
        return False


def _currency(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value in ('GBp', 'GBX', 'GBx'):
        return 'GBp'
    return value.upper() if re.fullmatch(r'[A-Za-z]{3}', value) else None


def _major_currency(value):
    value = _currency(value)
    return 'GBP' if value == 'GBp' else value


def _asof(value):
    if not _positive(value) or value < 315532800 or value > time.time() + 300:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def fetch_market_quote(symbol: str):
    """Checked metadata including provider exchange time, never retrieval time."""
    if not isinstance(symbol, str) or not re.fullmatch(r'[A-Za-z0-9.^=\-]{1,30}', symbol):
        return _failure('invalid_symbol')
    data = _get_json(_CHART + urllib.parse.quote(symbol, safe='') + '?interval=1d&range=5d')
    try:
        if data['chart'].get('error'):
            return _failure('provider_quote_error')
        meta = data['chart']['result'][0]['meta']
        ccy, asof = _currency(meta.get('currency')), _asof(meta.get('regularMarketTime'))
        if meta.get('symbol') != symbol or not ccy or not asof or not _positive(meta.get('regularMarketPrice')):
            return _failure('invalid_quote_metadata')
        return {'symbol': symbol, 'currency': ccy, 'price': float(meta['regularMarketPrice']), 'asof': asof,
                'exchange': meta.get('exchangeName'), 'instrument_type': meta.get('instrumentType'),
                'name': meta.get('longName') or meta.get('shortName'),
                'source_url': 'https://finance.yahoo.com/quote/' + urllib.parse.quote(symbol, safe='') + '/'}
    except (TypeError, KeyError, IndexError, AttributeError):
        return _failure(getattr(_local, 'error', None) or 'invalid_quote_metadata')


def fetch_price(symbol: str):
    """Compatibility helper; valuation uses fetch_market_quote for its time."""
    result = fetch_market_quote(symbol)
    return (result['price'], result['currency']) if result else (None, None)


def _resolve_listing(isin, ticker=None, price_currency=None):
    if not isinstance(isin, str) or not re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}[0-9]', isin):
        return _failure('invalid_isin')
    ticker = ticker.strip().upper() if isinstance(ticker, str) and ticker.strip() else None
    if ticker and not re.fullmatch(r'[A-Z0-9.\-]{1,20}', ticker):
        return _failure('invalid_ticker')
    expected_unit = _currency(price_currency)
    expected_ccy = _major_currency(price_currency)
    catalog = _LISTINGS.get(isin)
    if catalog:
        source, default, all_lines = catalog
        lines = all_lines.get(ticker or default, [])
        if expected_ccy:
            lines = [line for line in lines if _major_currency(line[1]) == expected_ccy]
        if len(lines) == 1:
            symbol, currency, exchange = lines[0]
            return {'symbol': symbol, 'currency': currency, 'exchange': exchange,
                    'basis': 'issuer_listing', 'identity_source': source}
        if ticker and not lines:
            return _failure('listing_hint_conflict')
    url = _SEARCH + urllib.parse.urlencode({'q': isin, 'quotesCount': 10, 'newsCount': 0, 'enableFuzzyQuery': 'false'})
    data = _get_json(url)
    if not isinstance(data, dict):
        return None
    rows = data.get('quotes')
    if not isinstance(rows, list):
        return _failure('invalid_search_metadata')
    candidates = [q for q in rows if isinstance(q, dict)
                  and q.get('quoteType') in ('EQUITY', 'ETF') and isinstance(q.get('symbol'), str)]
    if ticker:
        candidates = [q for q in candidates if q['symbol'].upper() == ticker or q['symbol'].upper().split('.')[0] == ticker]
    unique = {q['symbol']: q for q in candidates}
    if len(unique) != 1:
        return _failure('ambiguous_or_missing_listing')
    candidate = next(iter(unique.values()))
    if catalog:
        matches = [line for lines in catalog[2].values() for line in lines if line[0] == candidate['symbol']]
        if not matches:
            return _failure('listing_hint_conflict')
        _, currency, exchange = matches[0]
        return {'symbol': candidate['symbol'], 'currency': currency, 'exchange': exchange,
                'basis': 'issuer_equivalent_listing', 'identity_source': catalog[0]}
    return {'symbol': candidate['symbol'], 'currency': expected_unit, 'exchange': candidate.get('exchange'),
            'basis': 'isin_search_and_ticker' if ticker else 'unique_isin_search', 'identity_source': url}


def resolve_symbol(isin, ticker=None, price_currency=None):
    result = _resolve_listing(isin, ticker, price_currency)
    return result['symbol'] if result else None


def current_fx_to_gbp(ccy):
    """GBP per native unit, with actual FX time; pence is a unit conversion."""
    ccy = _currency(ccy)
    if ccy in ('GBP', 'GBp'):
        return {'rate_to_gbp': 0.01 if ccy == 'GBp' else 1.0, 'asof': None, 'source_url': None, 'symbol': None}
    if not ccy:
        return _failure('missing_currency')
    with _fx_lock:
        cached = _fx_cache.get(ccy)
        if cached and time.monotonic() - cached[0] < (_FX_TTL if cached[1] else 60):
            return dict(cached[1]) if cached[1] else _failure('fx_unavailable')
        pair = f'GBP{ccy}=X'  # native units per GBP; invert to GBP per native unit
        data = fetch_market_quote(pair)
        result = None
        if (data and data['currency'] == ccy and data['symbol'] == pair
                and data['instrument_type'] == 'CURRENCY' and data['exchange'] == 'CCY'):
            result = {'rate_to_gbp': 1.0 / data['price'], 'asof': data['asof'],
                      'source_url': data['source_url'], 'symbol': pair}
        _fx_cache[ccy] = (time.monotonic(), result)
        return dict(result) if result else _failure('fx_unavailable')


def _fx_to_gbp(ccy):
    result = current_fx_to_gbp(ccy)
    return result['rate_to_gbp'] if result else None


def quote(isin, symbol=None, *, ticker=None, price_currency=None):
    """An old cached symbol is never identity proof; resolve public ISIN/hints."""
    _local.error = None
    listing = _resolve_listing(isin, ticker, price_currency)
    if not listing:
        return None
    data = fetch_market_quote(listing['symbol'])
    if not data:
        return None
    if data['instrument_type'] not in ('EQUITY', 'ETF'):
        return _failure('wrong_instrument_type')
    if listing['exchange'] and data['exchange'] != listing['exchange']:
        return _failure('exchange_mismatch')
    # A London listing can quote in GBP or GBp. Treating these as the same
    # metadata unit would admit a 100x error when a provider mislabels a price.
    # Catalog entries carry the verified provider quote unit; imported execution
    # currency is used for other exact-ticker listings.
    if listing['currency'] and _currency(listing['currency']) != data['currency']:
        return _failure('quote_currency_mismatch')
    fx = current_fx_to_gbp(data['currency'])
    if not fx:
        return None
    value = data['price'] * fx['rate_to_gbp']
    if not _positive(value):
        return _failure('invalid_converted_price')
    return {'isin': isin, 'symbol': data['symbol'], 'currency': data['currency'], 'price_native': data['price'],
            'price_gbp': value, 'quote_asof': data['asof'], 'fetched_at': datetime.now(timezone.utc).isoformat(),
            'fx_rate_to_gbp': fx['rate_to_gbp'], 'fx_asof': fx['asof'], 'provider': 'Yahoo Finance',
            'source_url': data['source_url'], 'identity_verified': 1,
            'quote_meta': json.dumps({'resolution': listing, 'exchange': data['exchange'], 'name': data['name'],
                                     'instrument_type': data['instrument_type'], 'fx_symbol': fx['symbol'],
                                     'fx_source_url': fx['source_url']})}


class QuoteBatch(dict):
    def __init__(self):
        super().__init__()
        self.errors = {}


def batch_quote(items, workers=4):
    """Dict hints or legacy (ISIN,cached_symbol) tuples; no database writes."""
    out = QuoteBatch()
    def run(item):
        args = item if isinstance(item, dict) else {'isin': item[0], 'symbol': item[1]}
        try:
            result = quote(**args)
            return args['isin'], result, getattr(_local, 'error', None)
        except Exception:
            return args.get('isin'), None, 'quote_unavailable'
    if items:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, 4, len(items)))) as executor:
            for isin, result, error in executor.map(run, items):
                if result:
                    out[isin] = result
                else:
                    out.errors[isin] = error or 'quote_unavailable'
    return out


def batch_reference_fx(items):
    """ECB daily estimates, GBP per native unit, at most 7-day carry-forward.

    Explicit refresh only: persist successes separately from actual broker costs.
    Each currency/year is one bounded request; never one request per trade.
    """
    grouped = {}
    for ccy, day in items:
        currency = _major_currency(ccy)
        try:
            requested = date.fromisoformat(day)
        except (ValueError, TypeError):
            continue
        if currency and date(1999, 1, 4) <= requested <= datetime.now(timezone.utc).date():
            grouped.setdefault(currency, set()).add(requested)
    results = {}
    for currency, days in grouped.items():
        for year in sorted({d.year for d in days}):
            wanted = sorted(d for d in days if d.year == year)
            start, end = wanted[0] - timedelta(days=7), wanted[-1]
            url = _REFERENCE + urllib.parse.urlencode({'from': start.isoformat(), 'to': end.isoformat(),
                    'base': currency, 'quotes': 'GBP', 'providers': 'ecb'})
            rows = _get_json(url) if currency != 'GBP' else []
            if not isinstance(rows, list):
                continue
            valid = {}
            for row in rows:
                try:
                    observed = date.fromisoformat(row['date'])
                    if (row['base'] == currency and row['quote'] == 'GBP' and _positive(row['rate'])
                            and start <= observed <= end):
                        valid[observed] = float(row['rate'])
                except (TypeError, KeyError, ValueError):
                    continue
            for requested in wanted:
                prior = [d for d in valid if timedelta(0) <= requested - d <= timedelta(days=7)]
                if currency != 'GBP' and not prior:
                    continue
                observed = max(prior) if prior else requested
                results[(currency, requested.isoformat())] = {
                    'currency': currency, 'date': requested.isoformat(), 'requested_date': requested.isoformat(),
                    'observed_date': observed.isoformat(), 'rate_to_gbp': valid[observed] if prior else 1.0,
                    'source': 'ECB via Frankfurter' if currency != 'GBP' else 'GBP identity',
                    'source_url': url if currency != 'GBP' else None, 'fetched_at': datetime.now(timezone.utc).isoformat()}
    return results


def historical_reference_fx(currency, day):
    return batch_reference_fx([(currency, day)]).get((_major_currency(currency), day))

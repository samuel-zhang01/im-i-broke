"""Native-money reporting alongside the preserved GBP statement ledger.

Conversions are dated estimates with inspectable sources. No request to read a
report calls a provider, changes a transaction or substitutes zero for unknown FX.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from . import analytics, db, workspace
from .account_settings import effective_settings

router = APIRouter(prefix='/api/workspace', tags=['Currency reports'])
CURRENCIES = set('AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN BWP BYN BZD CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD UYU UZS VES VND VUV WST XAF XCD XOF XPF YER ZAR ZMW ZWG'.split())


def currency_code(value):
    if value not in CURRENCIES:
        raise ValueError('Choose a supported three-letter currency code, such as GBP, EUR or USD.')
    return value


def currency_precision(code):
    if code in {'BHD','IQD','JOD','KWD','LYD','OMR','TND'}:
        return 3
    if code in {'BIF','CLP','DJF','GNF','ISK','JPY','KMF','KRW','PYG','RWF','UGX','VND','VUV','XAF','XOF','XPF'}:
        return 0
    return 2


def currency_quantum(code):
    return Decimal(1).scaleb(-currency_precision(code))


def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS workspace_fx_rates (
        currency TEXT NOT NULL, observed_date TEXT NOT NULL, rate_to_gbp REAL NOT NULL CHECK(rate_to_gbp>0),
        evidence TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(currency,observed_date))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS native_transaction_reviews (
        bank_transaction_id INTEGER PRIMARY KEY REFERENCES bank_transactions(id),
        evidence_version TEXT NOT NULL, category_id TEXT REFERENCES category_catalog(id),
        spending_treatment TEXT NOT NULL, excluded INTEGER NOT NULL,
        overlap_decision TEXT, note TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')


class DatedRates:
    def __init__(self, conn):
        self.rates = defaultdict(dict)
        for row in conn.execute('SELECT currency,observed_date,rate_to_gbp,source,source_url FROM reference_fx ORDER BY date'):
            item = dict(row)
            if item['rate_to_gbp'] > 0 and math.isfinite(item['rate_to_gbp']):
                self.rates[item['currency']][item['observed_date']] = item
        if workspace.exists(conn, 'workspace_fx_rates'):
            for row in conn.execute('SELECT * FROM workspace_fx_rates'):
                self.rates[row['currency']][row['observed_date']] = dict(row) | {'source': 'User-supplied reference', 'source_url': None}
        self.days = {code: sorted(items) for code, items in self.rates.items()}

    def reference(self, code, when):
        if code == 'GBP':
            return {'currency': code, 'observed_date': when, 'rate_to_gbp': 1.0, 'source': 'GBP identity', 'source_url': None}
        days = self.days.get(code, [])
        index = bisect_right(days, when) - 1
        if index < 0 or (date.fromisoformat(when) - date.fromisoformat(days[index])).days > 7:
            return None
        return self.rates[code][days[index]]

    def convert(self, value, original, target, when):
        if original == target:
            return value, {'rate': 1.0, 'date': when, 'source': 'Same currency'}
        source, destination = self.reference(original, when), self.reference(target, when)
        if source is None or destination is None:
            return None, None
        rate = source['rate_to_gbp'] / destination['rate_to_gbp']
        converted = value * rate
        if not math.isfinite(rate) or not math.isfinite(converted):
            return None, None
        return converted, {'rate': rate, 'date': when, 'source': 'Dated reference estimate',
                              'original_reference': source, 'reporting_reference': destination}


def _category_name(mapping, name):
    return mapping.get((name or '').casefold(), {}).get('name', name)


def native_events(conn, *, category_maps=None):
    by_id, catalog = category_maps if category_maps is not None else workspace.category_maps(conn)
    settings = {row[0]: effective_settings(conn, row[0]) for row in conn.execute('SELECT id FROM accounts')}
    events = []
    for raw in conn.execute('''SELECT t.*,a.account_type,a.display_name AS account_name,a.bank
        FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE t.txn_date IS NOT NULL'''):
        row = dict(raw)
        if row['account_type'] == 'investment':
            continue  # Broker performance and trade cash are separate reports.
        workspace.annotate_category(row, catalog)
        row['category'] = _category_name(catalog, row['category'])
        account = settings[row['account_id']]
        row.update(event_id='ledger:' + str(row['id']), currency=row.get('currency') or 'GBP',
                   source='ledger', account_excluded=account['archived'] or not account['include_in_reports'],
                   account_ownership_share=account['ownership_share'])
        events.append(row)
    # Foreign bank activity is already retained faithfully outside the old GBP
    # ledger. Include only booked, mapped, unreconciled-native rows. A possible
    # CSV overlap is withheld instead of counted a second time.
    if workspace.exists(conn, 'bank_transactions'):
        from . import merchants
        aliases = merchants._prepared(conn)
        has_broker = bool(conn.execute("SELECT 1 FROM accounts WHERE account_type='investment' AND lower(replace(bank,' ',''))='trading212'").fetchone())
        rules = []
        for rule in conn.execute('SELECT * FROM rules ORDER BY priority,id'):
            try:
                rules.append((re.compile(rule['pattern'] if rule['is_regex'] else re.escape(rule['pattern']), re.I), rule['category']))
            except re.error:
                continue
        reviews = {row['bank_transaction_id']: dict(row) for row in conn.execute('SELECT * FROM native_transaction_reviews')} if workspace.exists(conn, 'native_transaction_reviews') else {}
        for raw in conn.execute('''SELECT b.*,c.account_id,c.amount_multiplier,a.account_type,a.display_name AS account_name
            FROM bank_transactions b JOIN bank_connections c ON c.id=b.connection_id
            JOIN accounts a ON a.id=c.account_id
            WHERE (b.status='review' OR (b.transaction_id IS NULL AND b.status='native')) AND b.is_pending=0 AND c.excluded=0'''):
            row = dict(raw)
            if row['account_type'] == 'investment':
                continue
            amount = row['amount'] * row['amount_multiplier']
            overlaps = [tuple(item) for item in conn.execute("SELECT id,dedupe_key,txn_date,amount,COALESCE(currency,'GBP') FROM transactions WHERE account_id=? AND COALESCE(currency,'GBP')=? AND txn_date=? AND ABS(amount-?)<0.005 ORDER BY id",
                                   (row['account_id'], row['currency'], row['txn_date'], amount))]
            from .categorize import auto_category
            category = _category_name(catalog, auto_category(row['description'] or '', row['merchant'] or '', amount))
            account = settings[row['account_id']]
            row.update(event_id='bank:' + str(row['id']), amount=amount, category=category,
                       source='bank_feed', merchant_kind='unknown', spending_treatment='normal',
                       transfer_group=None, exclude_from_analysis=0, classification=None, meta=None,
                       account_excluded=account['archived'] or not account['include_in_reports'],
                       account_ownership_share=account['ownership_share'], possible_overlap=bool(overlaps),
                       source_review=row['status'] == 'review', _has_broker_account=has_broker)
            row['evidence_version'] = hashlib.sha256(json.dumps(
                [[row.get(key) for key in ('id','account_id','txn_date','amount','currency','description','merchant','status')], overlaps],
                ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
            identity = merchants.classify({**row, 'source_merchant': row.get('merchant')}, aliases)
            if identity:
                identity['category'] = identity['category'] or row['category']
                row.update(identity)
            for pattern, rule_category in rules:
                if pattern.search(f"{row.get('description') or ''} {raw['merchant'] or ''}"):
                    row['category'] = rule_category
                    break
            review = reviews.get(row['id'])
            row['review_stale'] = bool(review and review['evidence_version'] != row['evidence_version'])
            row['reviewed'] = bool(review and not row['review_stale'])
            if row['reviewed']:
                reviewed_category = by_id.get(review['category_id'])
                if reviewed_category:
                    row['category'] = reviewed_category['name']
                row.update(spending_treatment=review['spending_treatment'], exclude_from_analysis=review['excluded'],
                           overlap_decision=review['overlap_decision'], review_note=review['note'])
            row['category'] = _category_name(catalog, row['category'])
            workspace.annotate_category(row, catalog)
            events.append(row)
    return events


class ReportChangedError(ValueError):
    """The requested evidence page belongs to a different report snapshot."""


def cashflow_report(conn, *, currency=None, date_from=None, date_to=None, account=None, offset=0, revision=None):
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0 or offset > 100000:
        raise ValueError('Choose a report offset between zero and 100,000.')
    target = currency_code(currency or workspace.preferences(conn)['reporting_currency'])
    if date_from and date_to and date_from > date_to:
        raise ValueError('Start date must not follow end date.')
    today = workspace.reporting_today(conn)
    end = date_to or today.isoformat()
    if account is not None:
        if not account or len(account) > 100:
            raise ValueError('Select between one and 100 account IDs.')
        account = set(account)
        known_accounts = {row[0] for row in conn.execute('SELECT id FROM accounts')}
        if not account <= known_accounts:
            raise ValueError('A selected account no longer exists. Reload account choices.')
    category_maps = workspace.category_maps(conn)
    selected = [row for row in native_events(conn, category_maps=category_maps)
                if (not account or row['account_id'] in account)
                and (not date_from or row['txn_date'] >= date_from) and row['txn_date'] <= end]
    if len(selected) > 100000:
        raise ValueError('Choose a shorter report period (at most 100,000 recorded events).')
    rates = DatedRates(conn)
    totals = {'income': 0.0, 'gross_spending': 0.0, 'refunds': 0.0, 'spending': 0.0, 'net': 0.0}
    months, categories, native_totals = {}, {}, {}
    omitted, items = [], []
    for row in selected:
        public = {key: row.get(key) for key in ('event_id', 'account_id', 'account_name', 'txn_date', 'description', 'merchant', 'category', 'source', 'currency',
                                                'evidence_version', 'reviewed', 'review_stale', 'review_note', 'overlap_decision', 'possible_overlap', 'spending_treatment', 'exclude_from_analysis', 'source_review', 'review_reason')}
        public.update(native_amount=row['amount'], ownership_share=row['account_ownership_share'], reporting_amount=None, fx=None, included=False)
        items.append(public)
        if row.get('source_review') and not row.get('account_excluded'):
            reason = 'Bank source evidence needs review: ' + (row.get('review_reason') or 'Check this transaction in Banks & accounts.')
            omitted.append({'event_id':row['event_id'], 'reason':reason, 'currency':row['currency'], 'amount':row['amount']})
            public['omission_reason'] = reason
            continue
        component = analytics._household_component(row)
        if component is None:
            omitted.append({'event_id': row['event_id'], 'reason': analytics._exclusion_reason(row), 'currency': row['currency'], 'amount': row['amount']})
            public['omission_reason'] = analytics._exclusion_reason(row)
            continue
        if component.get('analysis_component') == 'Configured household ownership share':
            # Keep the native contribution's precision until the final report
            # currency is known; the legacy GBP report keeps its own rounding.
            component['amount'] = row['amount'] * row['account_ownership_share']
        if row.get('review_stale'):
            reason = 'Provider evidence changed after review; review this transaction again.'
            omitted.append({'event_id': row['event_id'], 'reason': reason, 'currency': row['currency'], 'amount': row['amount']})
            public['omission_reason'] = reason
            continue
        if row.get('possible_overlap') and row.get('overlap_decision') != 'distinct':
            if row.get('overlap_decision') == 'duplicate':
                reason = 'Confirmed duplicate source'
                omitted.append({'event_id': row['event_id'], 'reason': reason, 'currency': row['currency'], 'amount': row['amount']})
                public['omission_reason'] = reason
                continue
            reason = 'Possible duplicate source; reconcile the account before including it.'
            omitted.append({'event_id': row['event_id'], 'reason': reason, 'currency': row['currency'], 'amount': row['amount']})
            public['omission_reason'] = reason
            continue
        value, evidence = rates.convert(component['amount'], row['currency'], target, row['txn_date'])
        native = native_totals.setdefault(row['currency'], {'income': 0.0, 'spending': 0.0})
        refund = component['is_refund']
        native['spending' if component['amount'] < 0 or refund else 'income'] += -component['amount'] if component['amount'] < 0 or refund else component['amount']
        public.update(reporting_amount=round(value, currency_precision(target)) if value is not None else None, fx=evidence, included=value is not None,
                      analysis_component=component.get('analysis_component'))
        if value is None:
            omitted.append({'event_id': row['event_id'], 'reason': 'Missing dated exchange rate', 'currency': row['currency'], 'amount': row['amount']})
            public['omission_reason'] = 'Missing dated exchange rate'
            continue
        month = months.setdefault(row['txn_date'][:7], dict(totals, month=row['txn_date'][:7]))
        # New monthly cells must start at zero, not the running household total.
        if 'initialized' not in month:
            month.update({key: 0.0 for key in totals})
            month['initialized'] = True
        category_name = _category_name(category_maps[1], component['category'] or 'Uncategorized')
        category = categories.setdefault(category_name, {'category': category_name, 'spending': 0.0})
        for bucket in (totals, month):
            if value < 0:
                bucket['gross_spending'] -= value
            elif refund:
                bucket['refunds'] += value
            else:
                bucket['income'] += value
            bucket['spending'] = bucket['gross_spending'] - bucket['refunds']
            bucket['net'] = bucket['income'] - bucket['spending']
        if value < 0 or refund:
            category['spending'] -= value
    def rounded(item, code=target):
        return {key: round(value, currency_precision(code)) if isinstance(value, float) else value for key, value in item.items() if key != 'initialized'}
    missing_fx = sum(item['reason'] == 'Missing dated exchange rate' for item in omitted)
    overlaps = sum(item['reason'].startswith('Possible duplicate') for item in omitted)
    stale_reviews = sum(item['reason'].startswith('Provider evidence changed') for item in omitted)
    source_reviews = sum(item['reason'].startswith('Bank source evidence needs review') for item in omitted)
    from . import account_coverage
    covered_accounts, intervals, gaps, sources = account_coverage.state(conn, planning=False)
    if account:
        covered_accounts = [item for item in covered_accounts if item['id'] in account]
    coverage = {}
    import calendar
    for key in months:
        year, number = map(int, key.split('-'))
        left, right = date(year, number, 1), date(year, number, calendar.monthrange(year, number)[1])
        missing, required = account_coverage.missing(covered_accounts, intervals, gaps, left, right)
        coverage[key] = {'complete': bool(required) and not missing and right < today
                         and (not date_from or date_from <= left.isoformat()) and end >= right.isoformat(), 'missing_accounts': missing, 'sources': sources}
    # A unique tie-breaker keeps every same-day event reachable, including
    # omitted evidence. Pagination never changes full-period totals.
    ordered = sorted(items, key=lambda item: (item['txn_date'], item['event_id']), reverse=True)
    report_revision = hashlib.sha256(json.dumps(
        {'currency': target, 'date_from': date_from, 'date_to': end, 'account': sorted(account) if account else None,
         'events': ordered}, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    if offset and revision != report_revision:
        raise ReportChangedError('Report evidence changed or its revision is missing. Return to the first evidence page to review the updated report.')
    page = ordered[offset:offset + 200]
    return {'currency': target, 'date_from': date_from, 'date_to': end, 'totals': rounded(totals),
            'monthly': [rounded(months[key]) for key in sorted(months)],
            'categories': [rounded(item) for item in sorted(categories.values(), key=lambda item: -item['spending'])],
            'native_totals': {key: rounded(value, key) for key, value in native_totals.items()},
            'events': page, 'offset': offset, 'limit': 200, 'report_revision': report_revision,
            'next_offset': offset + len(page) if offset + len(page) < len(items) else None,
            'event_count': len(items), 'omitted_count': len(omitted), 'omitted': omitted[:200],
            'missing_fx_count': missing_fx, 'possible_overlap_count': overlaps, 'stale_review_count': stale_reviews,
            'source_review_count': source_reviews, 'partial': bool(missing_fx or overlaps or stale_reviews or source_reviews),
            'coverage': coverage,
            'method': 'Booked banking and manual/imported transactions. Native amounts are retained; reporting amounts use dated reference estimates with a maximum seven-day carry. Pending entries, transfers, investment trades and excluded accounts are omitted. Missing rates and possible source overlaps are never treated as zero.'}


def _open():
    if not db.DB_PATH.is_file():
        raise HTTPException(503, 'The workspace database is unavailable.')
    return db.connect(db.DB_PATH)


@router.get('/reports/cashflow')
def report(currency: str | None = None, date_from: date | None = None, date_to: date | None = None,
           account: list[str] | None = Query(default=None), offset: int = Query(default=0, ge=0, le=100000),
           revision: str | None = Query(default=None, pattern=r'^[a-f0-9]{64}$')):
    with closing(_open()) as conn:
        # Pin all evidence, settings and FX reads to one consistent SQLite snapshot.
        conn.execute('BEGIN')
        try:
            return cashflow_report(conn, currency=currency, date_from=date_from.isoformat() if date_from else None,
                                   date_to=date_to.isoformat() if date_to else None, account=account, offset=offset, revision=revision)
        except ReportChangedError as error:
            raise HTTPException(409, str(error)) from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None


class NativeReviewInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    evidence_version: str = Field(pattern=r'^[a-f0-9]{64}$')
    category: str = Field(min_length=1, max_length=120)
    spending_treatment: Literal['normal','self_transfer','reimbursement'] = 'normal'
    excluded: StrictBool = False
    overlap_decision: Literal['distinct','duplicate'] | None = None
    note: str = Field(default='', max_length=1000)


@router.put('/native-transactions/{ident}/review')
def review_native(ident: int, body: NativeReviewInput):
    with closing(_open()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        row = next((item for item in native_events(conn) if item['event_id'] == f'bank:{ident}'), None)
        if row is None:
            raise HTTPException(404, 'Native bank transaction not found.')
        if row.get('source_review'):
            raise HTTPException(409, 'Resolve the bank source evidence in Banks & accounts before classifying this transaction.')
        if row['evidence_version'] != body.evidence_version:
            raise HTTPException(409, 'Bank evidence changed. Reload the report before reviewing it.')
        category = workspace.category_maps(conn)[1].get(body.category.casefold())
        if not category:
            raise HTTPException(422, 'Choose a saved category or add it in Settings first.')
        if body.overlap_decision and (not row.get('possible_overlap') or len(body.note) < 8):
            raise HTTPException(422, 'Confirm an actual possible overlap and provide evidence of at least eight characters.')
        before = conn.execute('SELECT * FROM native_transaction_reviews WHERE bank_transaction_id=?', (ident,)).fetchone()
        conn.execute('''INSERT INTO native_transaction_reviews(bank_transaction_id,evidence_version,category_id,spending_treatment,excluded,overlap_decision,note)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(bank_transaction_id) DO UPDATE SET evidence_version=excluded.evidence_version,
            category_id=excluded.category_id,spending_treatment=excluded.spending_treatment,excluded=excluded.excluded,
            overlap_decision=excluded.overlap_decision,note=excluded.note,updated_at=CURRENT_TIMESTAMP''',
            (ident, body.evidence_version, category['id'], body.spending_treatment, int(body.excluded), body.overlap_decision, body.note))
        conn.execute('INSERT INTO workspace_changes(kind,item_id,before_json,after_json) VALUES(?,?,?,?)',
                     ('native_transaction_review', str(ident), json.dumps(dict(before)) if before else None, body.model_dump_json()))
        return {'ok': True}


@router.delete('/native-transactions/{ident}/review')
def reset_native_review(ident: int):
    with closing(_open()) as conn, conn:
        before = conn.execute('SELECT * FROM native_transaction_reviews WHERE bank_transaction_id=?', (ident,)).fetchone()
        if before is None:
            raise HTTPException(404, 'Saved review not found.')
        conn.execute('DELETE FROM native_transaction_reviews WHERE bank_transaction_id=?', (ident,))
        conn.execute('INSERT INTO workspace_changes(kind,item_id,before_json,after_json) VALUES(?,?,?,?)',
                     ('native_transaction_review', str(ident), json.dumps(dict(before)), None))
        return {'ok': True}


class FxInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    currency: str = Field(min_length=3, max_length=3)
    observed_date: date
    rate_to_gbp: float = Field(ge=1e-9, le=1e9, allow_inf_nan=False)
    evidence: str = Field(min_length=8, max_length=1000)


@router.post('/exchange-rates')
def save_rate(body: FxInput):
    try:
        currency_code(body.currency)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    with closing(_open()) as conn, conn:
        if body.currency == 'GBP' or body.observed_date > workspace.reporting_today(conn):
            raise HTTPException(422, 'Use a non-GBP currency and an observed date today or earlier.')
        ensure_schema(conn)
        conn.execute('INSERT INTO workspace_fx_rates(currency,observed_date,rate_to_gbp,evidence) VALUES(?,?,?,?) ON CONFLICT(currency,observed_date) DO UPDATE SET rate_to_gbp=excluded.rate_to_gbp,evidence=excluded.evidence,updated_at=CURRENT_TIMESTAMP',
                     (body.currency, body.observed_date.isoformat(), body.rate_to_gbp, body.evidence))
        return {'ok': True}


@router.post('/exchange-rates/refresh')
def refresh_rates():
    # Explicit refresh sends only currencies and dates, never account identifiers
    # or amounts. Bound requests by currency/year before starting network work.
    from .prices import batch_reference_fx
    with closing(_open()) as conn:
        target = workspace.preferences(conn)['reporting_currency']
        wanted = {(row['currency'], row['txn_date']) for row in native_events(conn) if row['currency'] != 'GBP'}
        if target != 'GBP':
            wanted.update((target, row['txn_date']) for row in native_events(conn))
        groups = {(code, day[:4]) for code, day in wanted}
        if len(groups) > 24:
            raise HTTPException(422, 'This history needs more than 24 currency/year requests. Add reference rates or refresh a smaller history with the operator tools.')
        result = batch_reference_fx(sorted(wanted))
        with conn:
            for item in result.values():
                conn.execute('''INSERT INTO reference_fx(date,currency,rate_to_gbp,source,observed_date,fetched_at,source_url)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(date,currency) DO UPDATE SET rate_to_gbp=excluded.rate_to_gbp,
                    source=excluded.source,observed_date=excluded.observed_date,fetched_at=excluded.fetched_at,source_url=excluded.source_url''',
                    tuple(item[key] for key in ('date','currency','rate_to_gbp','source','observed_date','fetched_at','source_url')))
        return {'requested': len(wanted), 'saved': len(result), 'missing': len(wanted) - len(result)}

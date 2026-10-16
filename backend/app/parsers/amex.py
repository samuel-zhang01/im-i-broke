"""American Express UK credit-card statements.

Purchases are cash out, payments/refunds cash in; balances represent money owed.
AMEX prints CR on a separate line and includes foreign-exchange fees in the GBP
charge. Foreign detail and rewards tables must never become extra transactions.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

import pdfplumber

from .base import ParsedStatement, RawTxn

MONTHS = {m: i for i, m in enumerate(
    ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'), 1)}
DATE_PAIR = re.compile(
    r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s*(\d{1,2})\s+'
    r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s*(\d{1,2})\s+(.+)$', re.I)
AMOUNT = re.compile(r'(-?[\d,]+\.\d{2})\s*(CR)?\s*$', re.I)
MONEY = r'£\s*(-?[\d,]+\.\d{2})\s*(CR)?'


def _money(value: str, credit: str | None = None) -> float:
    amount = float(Decimal(value.replace(',', '')))
    return -abs(amount) if credit else amount


def _date(month: str, day: str, end: date) -> date:
    """The transaction year is omitted. Resolve against the statement end."""
    month_n = MONTHS[month[:3].title()]
    year = end.year - (month_n > end.month)
    return date(year, month_n, int(day))


def _header_meta(text: str) -> dict:
    member = re.search(r'\b([xX\d]{4}-[xX\d]{6}-\d{5})\b', text)
    period = re.search(
        r'Statement\s*Period\s*From\s*(\d{1,2})\s*([A-Za-z]+)\s*(\d{4})?\s*'
        r'to\s*(\d{1,2})\s*([A-Za-z]+)\s*(\d{4})', text, re.I)
    if not member or not period:
        raise ValueError('AMEX statement is missing its membership number or statement period')
    sd, sm, sy, ed, em, ey = period.groups()
    end = date(int(ey), MONTHS[em[:3].title()], int(ed))
    start = date(int(sy), MONTHS[sm[:3].title()], int(sd)) if sy else _date(sm, sd, end)
    # Four separately printed summary totals: previous - credits + debits = closing.
    summary = re.search(MONEY + r'\s*-\s*' + MONEY + r'\s*\+\s*' + MONEY +
                        r'\s*=\s*' + MONEY, text, re.I)
    if not summary:
        raise ValueError('AMEX statement balance summary could not be read')
    vals = [_money(summary.group(i), summary.group(i + 1)) for i in (1, 3, 5, 7)]
    return {'member': member.group(1).lower(), 'start': start, 'end': end,
            'opening': vals[0], 'credits': abs(vals[1]), 'debits': abs(vals[2]), 'closing': vals[3]}


def _transactions(text: str, end: date) -> list[RawTxn]:
    txns: list[RawTxn] = []
    pending: RawTxn | None = None
    for raw in text.splitlines():
        line = raw.strip()
        m = DATE_PAIR.match(line)
        if m:
            mon, day, post_mon, post_day, rest = m.groups()
            amt = AMOUNT.search(rest)
            if amt is None:
                raise ValueError('AMEX transaction row has no readable GBP amount')
            description = rest[:amt.start()].strip()
            amount = abs(_money(amt.group(1)))
            credit = bool(amt.group(2))
            pending = RawTxn(
                txn_date=_date(mon, day, end), description=description,
                amount=amount if credit else -amount, type_code='CR' if credit else 'DR',
                category='Transfers & payments' if 'PAYMENT RECEIVED' in description.upper() else None,
                raw=line, meta={'posted_date': _date(post_mon, post_day, end).isoformat()})
            txns.append(pending)
        elif line == 'CR' and pending is not None:
            pending.amount = abs(pending.amount)
            pending.type_code = 'CR'
            pending.raw += ' CR'
        elif pending is not None and 'Nonsterling Transaction Fee' in line:
            fee = re.search(r'Nonsterling Transaction Fee\s*([\d,]*\.\d{2})', line)
            if fee:
                pending.meta['fx_fee_included_gbp'] = float(fee.group(1).replace(',', ''))
        elif line.startswith(('Total new spend', 'How you can pay', 'Prepared for', 'Statement of Account')):
            pending = None
    return txns


def parse(path: str) -> ParsedStatement:
    with pdfplumber.open(path) as pdf:
        if not pdf.pages:
            raise ValueError('Empty AMEX PDF')
        meta = _header_meta(pdf.pages[0].extract_text() or '')
        txns = _transactions('\n'.join(p.extract_text() or '' for p in pdf.pages), meta['end'])
        for txn in txns:
            posted = date.fromisoformat(txn.meta['posted_date'])
            if not meta['start'] <= posted <= meta['end'] or txn.txn_date > posted:
                raise ValueError('AMEX transaction dates conflict with the printed statement period.')
    return ParsedStatement(
        bank='AMEX', account_type='credit', account_id=f"AMEX-credit-{meta['member'][-5:]}",
        sort_code=None, account_number=meta['member'], period_start=meta['start'], period_end=meta['end'],
        opening_balance=meta['opening'], closing_balance=meta['closing'], txns=txns, source_file=path,
        total_money_in=meta['credits'], total_money_out=meta['debits'])

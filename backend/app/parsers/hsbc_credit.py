"""Parser for HSBC credit card statements.

Layout: each transaction row has [posting date] [transaction date] [description]
[amount], with a single right-aligned Amount column (right edge ~545). Credits
(payments/refunds) carry a 'CR' suffix on the amount. FX detail sub-lines like
'24.37USD@1.3501' sit in the description area and are ignored for totals.

Sign convention (matches the debit parser): money out = negative, money in = positive.
Reconciliation: previous_balance + debits - credits == new_balance.
"""
from __future__ import annotations
import re
from datetime import datetime, date
from typing import Optional
import pdfplumber

from .base import RawTxn, ParsedStatement, UK_DATE_PATTERN, parse_uk_date

DATE_TOK = re.compile(r"^\d{2}$")
DATE_LINE = re.compile(rf"^({UK_DATE_PATTERN})\s+({UK_DATE_PATTERN})\s+(.*)", re.I)
AMT_TAIL = re.compile(r"([\d,]+\.\d{2})\s*(CR)?$", re.I)
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def _pdate(s: str) -> Optional[date]:
    try:
        return parse_uk_date(s)
    except (ValueError, TypeError):
        return None


def _header_meta(pdf) -> dict:
    text = pdf.pages[0].extract_text() or ""
    meta: dict = {}
    for key, pat in [("previous", r"PreviousBalance\s+([\d,]+\.\d{2})"),
                     ("debits", r"Debits\s+([\d,]+\.\d{2})"),
                     ("credits", r"Credits\s+([\d,]+\.\d{2})"),
                     ("new", r"NewBalance\s+([\d,]+\.\d{2})")]:
        m = re.search(pat, text.replace(" ", "") if key in ("previous", "new") else text)
        if m:
            meta[key] = float(m.group(1).replace(",", ""))
    # robust fallback on space-stripped text
    flat = text.replace(" ", "")
    for key, pat in [("previous", r"PreviousBalance([\d,]+\.\d{2})(CR)?"),
                     ("debits", r"Debits([\d,]+\.\d{2})"),
                     ("credits", r"Credits([\d,]+\.\d{2})"),
                     ("new", r"NewBalance([\d,]+\.\d{2})(CR)?")]:
        m = re.search(pat, flat)
        if m:
            val = float(m.group(1).replace(",", ""))
            if m.lastindex and m.lastindex >= 2 and m.group(2) == "CR":
                val = -val          # credit (overpaid) balance is negative owed
            meta[key] = val
    m = re.search(r"(\d{4} \d{4} \d{4} \d{4})", text)
    if m:
        meta["card"] = m.group(1).replace(" ", "")
    m = re.search(rf"Statement\s*Date\s*({UK_DATE_PATTERN})", text, re.I)
    if m:
        meta["statement_date"] = parse_uk_date(m.group(1))
    return meta


def parse(path: str) -> ParsedStatement:
    with pdfplumber.open(path) as pdf:
        meta = _header_meta(pdf)
        txns: list[RawTxn] = []
        transaction_first = False
        for p in pdf.pages:
            text = p.extract_text() or ""
            for ln in text.split("\n"):
                # Some exports reverse the two date columns. Honor the printed
                # labels; retain the established received/transaction layout
                # only when this page omits its header.
                header = ln.lower()
                transaction_label = re.search(r'(?:transaction\s*date|date\s*of\s*transaction)', header)
                posting_label = re.search(r'(?:posting\s*date|date\s*received|received\s*by\s*us)', header)
                if transaction_label and posting_label:
                    transaction_first = transaction_label.start() < posting_label.start()
                m = DATE_LINE.match(ln.strip())
                if not m:
                    continue
                first_s, second_s, rest = m.groups()
                txn_s, post_s = (first_s, second_s) if transaction_first else (second_s, first_s)
                am = AMT_TAIL.search(rest.strip())
                if not am:
                    raise ValueError('HSBC credit transaction has no readable amount.')
                posted, transacted = parse_uk_date(post_s), parse_uk_date(txn_s)
                if (meta.get('statement_date') and posted > meta['statement_date']) or transacted > posted:
                    raise ValueError('HSBC credit transaction dates conflict with the printed statement date.')
                val = float(am.group(1).replace(",", ""))
                is_credit = bool(am.group(2))
                desc = rest[:am.start()].strip()
                # strip leading card-network glyphs like ')))' or 'IAP'
                desc = re.sub(r"^[)\(]+\s*", "", desc)
                txns.append(RawTxn(
                    txn_date=transacted,
                    description=desc,
                    amount=val if is_credit else -val,
                    type_code="CR" if is_credit else "DR",
                    raw=ln.strip(),
                    meta={"posting_date": posted.isoformat(), "parser": "hsbc_credit",
                          "printed_transaction_date": txn_s, "printed_posting_date": post_s},
                ))
    debits = sum(-t.amount for t in txns if t.amount < 0)
    credits = sum(t.amount for t in txns if t.amount > 0)
    st = ParsedStatement(
        bank="HSBC", account_type="credit",
        account_id=f"HSBC-credit-{meta.get('card','')[-4:]}",
        sort_code=None, account_number=meta.get("card"),
        period_start=None, period_end=meta.get("statement_date"),
        opening_balance=meta.get("previous"), closing_balance=meta.get("new"),
        txns=txns, source_file=path,
        total_money_in=meta.get("credits"), total_money_out=meta.get("debits"),
        meta={
            "parser": "hsbc_credit",
            "printed_card_number": meta.get("card"),
            "printed_statement_date": meta["statement_date"].isoformat() if meta.get("statement_date") else None,
            "printed_debits": meta.get("debits"),
            "printed_credits": meta.get("credits"),
            # These dates corroborate a candidate interval; neither is used as
            # its start. Quiet days belong to a statement period too.
            "posting_dates_valid": all(t.meta["posting_date"] for t in txns),
            "first_posting_date": min((t.meta["posting_date"] for t in txns if t.meta["posting_date"]), default=None),
            "last_posting_date": max((t.meta["posting_date"] for t in txns if t.meta["posting_date"]), default=None),
        },
    )
    st._debits = debits  # type: ignore
    st._credits = credits  # type: ignore
    return st

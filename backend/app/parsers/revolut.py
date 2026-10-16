"""Parser for Revolut GBP account statements (personal + joint).

Revolut renders a clean tabular layout with right/left-aligned amount columns:
Date | Description | Money out | Money in | Balance. We cluster pdfplumber words
into visual lines, then classify each amount by its x-position into the
out / in / balance column. Continuation lines ('Reference:', 'From:', 'To:')
carry no date and are skipped. Each transaction row has exactly two amounts:
(money out OR money in) + the running balance.

Sign convention (matches the other parsers): money in = positive, out = negative.
Reconciliation: opening + sum(amounts) == closing. The per-row running balance
column gives an independent chain check (prev_balance + amount == row_balance).

Two distinct accounts exist in the data (different sort codes / numbers): a
personal account ('GBP Statement') and a joint account ('Joint Account GBP
Statement'); account_type distinguishes them so display names don't collide.
"""
from __future__ import annotations
import re
from datetime import datetime, date
from typing import Optional
import pdfplumber

from .base import RawTxn, ParsedStatement, cluster_lines

MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}

# Column boundaries by word-centre x (page width ~595). Date col ends ~118;
# description runs to ~320; Money out centres ~347, Money in ~432, Balance ~537.
X_DATE_MAX = 118.0
X_DESC_MAX = 320.0
X_OUT_MAX = 395.0       # centre < this -> Money out column
X_IN_MAX = 495.0        # centre < this -> Money in column, else Balance

AMT_RE = re.compile(r"^£?-?[\d,]+\.\d{2}$")
# Transaction rows begin '23 Jul 2025' / '1 Sept 2025'; month token is 3-4 chars.
DATE_RE = re.compile(r"^(\d{1,2}) ([A-Za-z]{3,4}) (\d{4})$")


def _centre(w: dict) -> float:
    return (w["x0"] + w["x1"]) / 2


def _amount(tok: str) -> Optional[float]:
    tok = tok.strip()
    if not AMT_RE.match(tok):
        return None
    return float(tok.replace("£", "").replace(",", ""))


def _txn_date(toks: list[str]) -> Optional[date]:
    # toks like ['23', 'Jul', '2025'] or ['1', 'Sept', '2025']
    try:
        d, mon, yy = int(toks[0]), MONTHS[toks[1][:3].title()], int(toks[2])
        return date(yy, mon, d)
    except Exception:
        return None


def _period_date(s: str) -> Optional[date]:
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _header_meta(pdf) -> dict:
    text = pdf.pages[0].extract_text() or ""
    meta: dict = {"joint": "Joint Account" in text}
    m = re.search(r"Sort Code\s+(\d{6})", text)
    if m:
        d = m.group(1)
        meta["sort_code"] = f"{d[0:2]}-{d[2:4]}-{d[4:6]}"
    m = re.search(r"Account\s+Number\s+(\d{6,8})", text) or re.search(r"Account\s+(\d{6,8})", text)
    if m:
        meta["account_number"] = m.group(1)
    m = re.search(r"IBAN\s+(GB[0-9A-Z]+)", text)
    if m:
        meta["iban"] = m.group(1)
    # Balance summary 'Total <opening> <money out> <money in> <closing>'
    m = re.search(r"Total\s+£([\d,]+\.\d{2})\s+£([\d,]+\.\d{2})\s+£([\d,]+\.\d{2})\s+£([\d,]+\.\d{2})", text)
    if m:
        meta["opening"] = float(m.group(1).replace(",", ""))
        meta["money_out"] = float(m.group(2).replace(",", ""))
        meta["money_in"] = float(m.group(3).replace(",", ""))
        meta["closing"] = float(m.group(4).replace(",", ""))
    m = re.search(r"from (\d{1,2} \w+ \d{4}) to (\d{1,2} \w+ \d{4})", text)
    if m:
        meta["period_start"] = _period_date(m.group(1))
        meta["period_end"] = _period_date(m.group(2))
    return meta


def _exchange_continuation(txn, text):
    """Keep printed FX fees separate without changing the bank's signed amount."""
    if not txn.meta or not txn.meta.get('currency_exchange'):
        return
    match = re.search(r'\bFee:\s*£([\d,]+\.\d{2})(?:\s+£([\d,]+\.\d{2}))?', text)
    if match:
        fee = float(match.group(1).replace(',', ''))
        principal = abs(txn.amount) - fee if txn.amount < 0 else abs(txn.amount) + fee
        if principal < 0:
            raise ValueError('Exchange fee exceeds the statement withdrawal')
        if match.group(2) and txn.amount < 0 and abs(float(match.group(2).replace(',', '')) - principal) > .011:
            raise ValueError('Printed exchange principal and fee do not reconcile')
        txn.meta.update(exchange_fee_gbp=fee, exchange_principal_gbp=round(principal, 2),
                        exchange_fee_evidence=text)


def parse(path: str) -> ParsedStatement:
    with pdfplumber.open(path) as pdf:
        meta = _header_meta(pdf)
        txns: list[RawTxn] = []
        for p in pdf.pages:
            for row in cluster_lines(p.extract_words()):
                date_toks = [w["text"] for w in row if _centre(w) < X_DATE_MAX]
                dm = DATE_RE.match(" ".join(date_toks[:3]))
                if not dm:
                    if txns:
                        _exchange_continuation(txns[-1], ' '.join(w['text'] for w in row))
                    continue  # continuation / summary / footer line
                d = _txn_date([dm.group(1), dm.group(2), dm.group(3)])

                out_val = in_val = bal_val = None
                for w in row:
                    v = _amount(w["text"])
                    if v is None:
                        continue
                    cx = _centre(w)
                    if cx < X_OUT_MAX:
                        out_val = v
                    elif cx < X_IN_MAX:
                        in_val = v
                    else:
                        bal_val = v
                if bal_val is None or (out_val is None and in_val is None):
                    continue  # not a real transaction row

                amount = in_val if in_val is not None else -out_val
                first_amt_x = min(_centre(w) for w in row if _amount(w["text"]) is not None)
                desc = " ".join(
                    w["text"] for w in row
                    if X_DATE_MAX <= _centre(w) < min(X_DESC_MAX, first_amt_x)
                    and _amount(w["text"]) is None
                ).strip()
                exchange = re.fullmatch(r'Exchanged (?:to|from) ([A-Z]{3})', desc, re.I)
                txn_meta = ({'currency_exchange': True, 'exchange_currency': exchange.group(1).upper(),
                             'exchange_fee_gbp': 0.0, 'exchange_principal_gbp': round(abs(amount), 2),
                             'exchange_fee_evidence': 'No separate fee printed for this exchange'} if exchange else None)
                txns.append(RawTxn(
                    txn_date=d, description=desc, amount=round(amount, 2),
                    balance=bal_val, type_code=None,
                    raw=f"{' '.join(date_toks[:3])} {desc}",
                    meta=txn_meta,
                ))

    acct = meta.get("account_number", "")
    account_type = "joint" if meta.get("joint") else "current"
    suffix = acct or (meta.get("iban", "")[-8:])
    return ParsedStatement(
        bank="Revolut", account_type=account_type,
        account_id=f"Revolut-{account_type}-{suffix}",
        sort_code=meta.get("sort_code"), account_number=acct or None,
        period_start=meta.get("period_start"), period_end=meta.get("period_end"),
        opening_balance=meta.get("opening"), closing_balance=meta.get("closing"),
        txns=txns, source_file=path,
        total_money_in=meta.get("money_in"), total_money_out=meta.get("money_out"),
    )

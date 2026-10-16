"""Parser for Lloyds Bank current account statements.

Lloyds PDFs embed an invisible per-row header watermark (white, size ~0.1)
interleaved with the real data (black, size ~9). We discard the watermark by
keeping only real-size black characters, rebuild lines, then read the columns by
x-position: Date | Description | Type | Money In | Money Out | Balance.
"""
from __future__ import annotations
import re
from datetime import datetime, date
from typing import Optional
import pdfplumber

from .base import RawTxn, ParsedStatement, parse_amount

MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}

# Column x-boundaries (left edge based)
COL_DESC = 118
COL_TYPE = 268
COL_IN = 320
COL_OUT = 385
COL_BAL = 465
DATE_RE = re.compile(r"^(\d{2}) (\w{3}) (\d{2})$")


def _real_chars(page):
    out = []
    for c in page.chars:
        col = c.get("non_stroking_color")
        is_black = col in [(0,), (0.0,), 0, 0.0] or col == (0, 0, 0)
        if c["size"] > 1.0 and is_black:
            out.append(c)
    return out


def _lines(chars, tol=2.5):
    rows: list[list[dict]] = []
    for c in sorted(chars, key=lambda c: (round(c["top"]), c["x0"])):
        for row in rows:
            if abs(row[0]["top"] - c["top"]) <= tol:
                row.append(c); break
        else:
            rows.append([c])
    for r in rows:
        r.sort(key=lambda c: c["x0"])
    rows.sort(key=lambda r: r[0]["top"])
    return rows


def _text_in(row, x0, x1):
    s = ""
    last_x = None
    for c in row:
        cx = (c["x0"] + c["x1"]) / 2
        if x0 <= cx < x1:
            if last_x is not None and c["x0"] - last_x > 1.8:
                s += " "
            s += c["text"]
            last_x = c["x1"]
    return s.strip()


def _header_meta(pdf) -> dict:
    text = pdf.pages[0].extract_text() or ""
    flat = text.replace(" ", "")
    meta: dict = {}
    m = re.search(r"SortCode(\d{2}-\d{2}-\d{2})", flat)
    if m: meta["sort_code"] = m.group(1)
    m = re.search(r"AccountNumber(\d{8})", flat)
    if m: meta["account_number"] = m.group(1)
    m = re.search(r"MoneyIn£([\d,]+\.\d{2})", flat)
    if m: meta["money_in"] = float(m.group(1).replace(",", ""))
    m = re.search(r"MoneyOut£([\d,]+\.\d{2})", flat)
    if m: meta["money_out"] = float(m.group(1).replace(",", ""))
    m = re.search(r"Balanceon(\d{2}\w+\d{4})£([\d,]+\.\d{2})", flat)
    if m: meta["opening"] = float(m.group(2).replace(",", ""))
    bals = re.findall(r"Balanceon\d{2}\w+\d{4}£([\d,]+\.\d{2})", flat)
    if len(bals) >= 2:
        meta["opening"] = float(bals[0].replace(",", ""))
        meta["closing"] = float(bals[1].replace(",", ""))
    m = re.search(r"(\d{2} \w+ \d{4}) to (\d{2} \w+ \d{4})", text)
    if m:
        for fmt in ("%d %B %Y", "%d %b %Y"):
            try:
                meta["period_start"] = datetime.strptime(m.group(1), fmt).date()
                meta["period_end"] = datetime.strptime(m.group(2), fmt).date()
                break
            except ValueError:
                pass
    return meta


def parse(path: str) -> ParsedStatement:
    with pdfplumber.open(path) as pdf:
        meta = _header_meta(pdf)
        txns: list[RawTxn] = []
        cur_date: Optional[date] = None
        for p in pdf.pages:
            for row in _lines(_real_chars(p)):
                date_s = _text_in(row, 40, COL_DESC)
                desc = _text_in(row, COL_DESC, COL_TYPE)
                typ = _text_in(row, COL_TYPE, COL_IN)
                m_in = _text_in(row, COL_IN, COL_OUT).replace(" ", "")
                m_out = _text_in(row, COL_OUT, COL_BAL).replace(" ", "")
                bal = _text_in(row, COL_BAL, 600).replace(" ", "")
                dm = DATE_RE.match(date_s)
                if dm:
                    cur_date = date(2000 + int(dm.group(3)), MONTHS[dm.group(2).title()], int(dm.group(1)))
                vin, vout = parse_amount(m_in), parse_amount(m_out)
                if vin is None and vout is None:
                    continue
                if not desc and not typ:
                    continue
                amount = vin if vin is not None else -vout
                txns.append(RawTxn(
                    txn_date=cur_date, description=desc, amount=amount,
                    balance=parse_amount(bal), type_code=typ or None, raw=f"{date_s} {desc} {typ}",
                ))
    acct = meta.get("account_number", "")
    # The Lloyds-quoted "Balance on <start>" already reflects day-1 activity, so
    # derive the true opening from the first transaction's running balance.
    if txns and txns[0].balance is not None:
        opening = round(txns[0].balance - txns[0].amount, 2)
        closing = next((t.balance for t in reversed(txns) if t.balance is not None),
                       meta.get("closing"))
    else:
        opening = meta.get("opening")
        closing = meta.get("closing")
    return ParsedStatement(
        bank="Lloyds", account_type="current", account_id=f"Lloyds-{acct}",
        sort_code=meta.get("sort_code"), account_number=acct or None,
        period_start=meta.get("period_start"), period_end=meta.get("period_end"),
        opening_balance=opening, closing_balance=closing,
        txns=txns, source_file=path,
        total_money_in=meta.get("money_in"), total_money_out=meta.get("money_out"),
    )

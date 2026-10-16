"""Parser for HSBC personal bank account (debit/current) statements.

Strategy: HSBC lays transactions in three right-aligned amount columns
(Paid out / Paid in / Balance). We recover the column x-positions from the
header row, then walk visual lines top-to-bottom. A new transaction begins at
a recognised type-code token in the 'type' column; description lines and the
amount that follows belong to the current transaction.
"""
from __future__ import annotations
import re
from datetime import datetime, date
from typing import Optional
import pdfplumber

from .base import RawTxn, ParsedStatement, parse_amount, cluster_lines, parse_uk_date

# A type code is a 2-4 char uppercase token in the 'type' column.
TYPE_COL_X = (105, 138)
DATE_RE = re.compile(r"^\d{1,2}$")
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def _money(num: float, paid_in: bool) -> float:
    return num if paid_in else -num


def _find_columns(pages) -> tuple[float, float, float]:
    """Return right-edge x of (paid_out, paid_in, balance) columns."""
    for p in pages:
        for w in p.extract_words():
            if "Paid" in w["text"] and w["x0"] > 300:
                # found header area; derive from known layout
                pass
    # HSBC layout is stable; use right edges with generous bands.
    return 410.0, 500.0, 600.0


def _classify_amount(x1: float) -> str:
    if x1 < 410:
        return "out"
    if x1 < 500:
        return "in"
    return "balance"


def _parse_date(tokens: list[str]) -> Optional[date]:
    # tokens like ['16','Apr','26']
    try:
        return parse_uk_date(' '.join(tokens))
    except (ValueError, TypeError):
        return None


def _table_date(value, meta, previous):
    explicit = _parse_date([value])
    if explicit:
        result = explicit
    else:
        partial = re.fullmatch(r'(\d{1,2})\s*([A-Za-z]{3,9})?', value.strip())
        start, end = meta.get('period_start'), meta.get('period_end')
        if not partial or not start or not end:
            raise ValueError('HSBC bank transaction date could not be read; no date was inferred.')
        day = int(partial[1])
        month = parse_uk_date(f'1 {partial[2]} 2000').month if partial[2] else None
        possibilities = []
        for year in range(start.year, end.year + 1):
            for candidate_month in ([month] if month else range(1, 13)):
                try:
                    candidate = date(year, candidate_month, day)
                except (ValueError, TypeError):
                    continue
                if start <= candidate <= end and (previous is None or candidate >= previous):
                    possibilities.append(candidate)
        if len(possibilities) != 1:
            raise ValueError('HSBC bank transaction date is ambiguous within the printed statement period.')
        result = possibilities[0]
    if ((meta.get('period_start') and result < meta['period_start']) or
            (meta.get('period_end') and result > meta['period_end'])):
        raise ValueError('HSBC bank transaction date is outside the printed statement period.')
    return result


def _header_meta(pdf) -> dict:
    text = pdf.pages[0].extract_text() or ""
    meta: dict = {}
    m = re.search(r"(\d{2}-\d{2}-\d{2})\s+(\d{8})", text)
    if m:
        meta["sort_code"] = m.group(1); meta["account_number"] = m.group(2)
    m = re.search(r"Opening\s*Balance\s*£?(-?[\d,]+\.\d{2})(D)?", text, re.I)
    if m: meta["opening"] = -abs(float(m.group(1).replace(",", ""))) if m.group(2) else float(m.group(1).replace(",", ""))
    m = re.search(r"Closing\s*Balance\s*£?(-?[\d,]+\.\d{2})(D)?", text, re.I)
    if m: meta["closing"] = -abs(float(m.group(1).replace(",", ""))) if m.group(2) else float(m.group(1).replace(",", ""))
    for key, label in (("money_in", "In"), ("money_out", "Out")):
        m = re.search(r"Payments\s*" + label + r"\s*£?([\d,]+\.\d{2})", text)
        if m:
            meta[key] = float(m.group(1).replace(",", ""))
    m = re.search(r"(\d{1,2}\s*[A-Za-z]{3,9}(?:\s*(?:\d{4}|\d{2}))?)\s+to\s+(\d{1,2}\s*[A-Za-z]{3,9}\s*(?:\d{4}|\d{2}))", text, re.I)
    if m:
        end = parse_uk_date(m.group(2))
        meta["period_end"] = end
        if end:
            explicit_start = _parse_date([m.group(1)])
            start = explicit_start or parse_uk_date(m.group(1) + f" {end.year}")
            if not explicit_start and start > end:
                start = start.replace(year=end.year - 1)
            if start > end:
                raise ValueError('HSBC statement period starts after its printed end date.')
            meta["period_start"] = start
    return meta


def _parse_date_words(s: str) -> Optional[date]:
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse(path: str) -> ParsedStatement:
    with pdfplumber.open(path) as pdf:
        meta = _header_meta(pdf)
        txns: list[RawTxn] = []
        cur_date: Optional[date] = None
        date_text = None
        desc_buf: list[str] = []
        type_buf: Optional[str] = None

        def emit(amount: float):
            nonlocal desc_buf, type_buf
            if cur_date is None:
                raise ValueError('HSBC bank transaction has no printed date.')
            txns.append(RawTxn(
                txn_date=cur_date,
                description=" ".join(desc_buf).strip(),
                amount=amount,
                balance=None,
                type_code=type_buf,
                raw=" ".join(desc_buf),
                meta={"parser": "hsbc_debit", "printed_date": date_text},
            ))
            desc_buf = []
            type_buf = None

        for p in pdf.pages:
            rows = cluster_lines(p.extract_words())
            in_table = False
            for row in rows:
                flat = " ".join(w["text"] for w in row).replace(" ", "")
                if "Paidout" in flat and "Paidin" in flat and "Balance" in flat:
                    in_table = True
                    continue
                if not in_table:
                    continue
                is_bf = "BALANCEBROUGHTFORWARD" in flat or "BALANCECARRIEDFORWARD" in flat
                # date detection (first tokens look like '16 Apr 26')
                dtoks = [w["text"] for w in row if w["x0"] < 105]
                if dtoks and re.match(r'^\d', dtoks[0]):
                    printed_date = ' '.join(dtoks)
                    if is_bf:
                        # Opening balances can be dated the day before this
                        # statement starts. They are not transactions and must
                        # not supply an out-of-period date to undated activity.
                        anchor = _parse_date([printed_date])
                        if (anchor and
                                (not meta.get('period_start') or anchor >= meta['period_start']) and
                                (not meta.get('period_end') or anchor <= meta['period_end'])):
                            cur_date, date_text = anchor, printed_date
                    else:
                        cur_date = _table_date(printed_date, meta, cur_date)
                        date_text = printed_date
                # type-code token in the type column
                type_tok = next((w for w in row if TYPE_COL_X[0] <= w["x0"] <= TYPE_COL_X[1]
                                 and w["text"].isalpha() and w["text"].isupper()
                                 and 2 <= len(w["text"]) <= 4), None)
                if type_tok and not is_bf:
                    type_buf = type_tok["text"]
                # process tokens left-to-right: build description, emit on amount
                for w in row:
                    val = parse_amount(w["text"])
                    if val is not None and w["x0"] > 300:
                        col = _classify_amount(w["x1"])
                        if col == "balance" or is_bf:
                            if col == "balance" and txns:
                                txns[-1].balance = val
                            continue
                        emit(_money(val, paid_in=(col == "in")))
                    elif 138 <= w["x0"] <= 340 and w["text"] != "." and not is_bf:
                        desc_buf.append(w["text"])
                if "BALANCECARRIEDFORWARD" in flat:
                    in_table = False

    acct = (meta.get("account_number") or "")
    return ParsedStatement(
        bank="HSBC", account_type="debit",
        account_id=f"HSBC-debit-{acct}", sort_code=meta.get("sort_code"),
        account_number=acct or None,
        period_start=meta.get("period_start"), period_end=meta.get("period_end"),
        opening_balance=meta.get("opening"), closing_balance=meta.get("closing"),
        txns=txns, source_file=path,
        total_money_in=meta.get("money_in"), total_money_out=meta.get("money_out"),
    )

"""Shared parsing utilities for bank statement PDFs."""
from __future__ import annotations
import re
from decimal import Decimal
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Optional

AMT_RE = re.compile(r"^-?[0-9][0-9,]*\.[0-9]{2}$")
UK_DATE_PATTERN = r'\d{1,2}\s*[A-Za-z]{3,9}\s*(?:\d{4}|\d{2})'
_MONTHS = {name.lower(): index for index, name in enumerate(
    ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'), 1)}
_MONTHS.update({name.lower(): index for index, name in enumerate(
    ('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
     'September', 'October', 'November', 'December'), 1)})


def parse_uk_date(value: str) -> date:
    """Read a printed calendar date, without truncating a four-digit year."""
    match = re.fullmatch(r'(\d{1,2})\s*([A-Za-z]{3,9})\s*(\d{4}|\d{2})', value.strip())
    if not match or match[2].lower() not in _MONTHS:
        raise ValueError('The printed statement date could not be read.')
    year = int(match[3])
    return date(2000 + year if len(match[3]) == 2 else year,
                _MONTHS[match[2].lower()], int(match[1]))


def parse_amount(tok: str) -> Optional[float]:
    tok = tok.strip()
    if not AMT_RE.match(tok):
        return None
    return float(tok.replace(",", ""))


@dataclass
class RawTxn:
    """A normalized transaction extracted from a statement."""
    txn_date: Optional[date]
    description: str
    amount: float            # signed: positive = money in, negative = money out
    balance: Optional[float] = None
    type_code: Optional[str] = None
    raw: str = ""
    category: Optional[str] = None   # parser-supplied category (overrides auto)
    ext_id: Optional[str] = None     # provider's unique id (used for dedup)
    merchant: Optional[str] = None   # parser-supplied merchant (e.g. ticker)
    meta: Optional[dict] = None      # parser extras (e.g. shares, isin, result)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["txn_date"] = self.txn_date.isoformat() if self.txn_date else None
        return d


@dataclass
class ParsedStatement:
    bank: str
    account_type: str            # 'debit' | 'credit' | 'current'
    account_id: str              # sort code + account / card number
    sort_code: Optional[str]
    account_number: Optional[str]
    period_start: Optional[date]
    period_end: Optional[date]
    opening_balance: Optional[float]
    closing_balance: Optional[float]
    txns: list[RawTxn] = field(default_factory=list)
    source_file: str = ""
    total_money_in: Optional[float] = None
    total_money_out: Optional[float] = None
    meta: Optional[dict] = None   # source evidence and derived statement provenance

    def reconciles(self, tol: float = 0.02) -> Optional[bool]:
        """Compare the ledger with independently reported statement totals.

        Credit balances are amounts owed, so purchases increase that balance.
        Missing balances mean unverified, including empty transaction exports.
        """
        if self.opening_balance is None or self.closing_balance is None:
            return None
        amounts = [Decimal(str(t.amount)) for t in self.txns]
        tolerance = Decimal(str(tol))
        for supplied, actual in (
            (self.total_money_in, sum((v for v in amounts if v > 0), Decimal(0))),
            (self.total_money_out, -sum((v for v in amounts if v < 0), Decimal(0))),
        ):
            if supplied is not None and abs(Decimal(str(supplied)) - actual) > tolerance:
                return False
        sign = -1 if self.account_type == "credit" else 1
        calc = Decimal(str(self.opening_balance)) + sign * sum(amounts, Decimal(0))
        return abs(calc - Decimal(str(self.closing_balance))) <= tolerance


def cluster_lines(words: list[dict], tol: float = 3.0) -> list[list[dict]]:
    """Group pdfplumber words into visual lines by their 'top' coordinate."""
    rows: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        placed = False
        for row in rows:
            if abs(row[0]["top"] - w["top"]) <= tol:
                row.append(w)
                placed = True
                break
        if not placed:
            rows.append([w])
    for row in rows:
        row.sort(key=lambda w: w["x0"])
    rows.sort(key=lambda r: r[0]["top"])
    return rows

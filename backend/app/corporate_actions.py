"""Effective-dated, sourced share adjustments, separate from the cash ledger.

The issuer's legal effective date and the first adjusted trading date are kept
separately. Splits are replayed before trades on ``ex_date``; quantities in the
source trade rows always remain in the units in which those trades executed.
No final holding quantity is stored or inferred from market value.
"""
from __future__ import annotations

import json
import math
from datetime import date
from urllib.parse import urlparse


KNOWN_SPLITS = ({
    "isin": "US67066G1040",
    "effective_date": "2024-06-07",
    "ex_date": "2024-06-10",
    "ratio_new": 10,
    "ratio_old": 1,
    "source_url": "https://www.sec.gov/Archives/edgar/data/1045810/000104581024000144/nvda-20240607.htm",
    "source_title": "NVIDIA Form 8-K, filed June 7, 2024, Item 5.03",
    "verified_at": "2026-09-15",
    "notes": "Effective 4:01 p.m. Eastern on June 7; split-adjusted trading June 10. "
             "Issuer event verified; reconstructed quantity still needs a broker holdings snapshot.",
},)


def validate_trade_revision(old_meta: str | None, new_meta: dict) -> None:
    """Reject a changed unit count for an already identified broker trade.

    Some data products rewrite old trades into today's split-adjusted units.
    Replacing execution units with those units would apply the split twice.
    A changed provider-ID trade must therefore be reviewed, not overwritten.
    Missing legacy metadata can still be filled by parser migrations.
    """
    try:
        old = json.loads(old_meta or "{}")
    except (TypeError, ValueError):
        return
    if not isinstance(old, dict):
        return
    if old.get("shares") is None or new_meta.get("shares") is None:
        return
    if (old.get("isin") != new_meta.get("isin") or
            not math.isclose(float(old["shares"]), float(new_meta["shares"]), rel_tol=1e-10, abs_tol=1e-10)):
        raise ValueError("An existing broker trade has changed security or share quantity. "
                         "Review possible split-adjusted history before importing it.")


def record_split(conn, account_id: str, *, isin: str, effective_date: str, ex_date: str,
                 ratio_new: int, ratio_old: int, source_url: str, source_title: str,
                 verified_at: str, notes: str | None = None) -> int:
    """Record one event idempotently; refuse a conflicting ratio/date on replay.

    A future supported broker action importer should call this same function,
    not add a second share transaction. Unsupported CSV actions are rejected by
    the parser until their share semantics and evidence have been verified.
    The caller owns the transaction and commit.
    """
    if not isin or not source_title or not urlparse(source_url).netloc or urlparse(source_url).scheme != "https":
        raise ValueError("A corporate action needs a security identity and HTTPS source")
    if any(type(value) is not int or value <= 0 for value in (ratio_new, ratio_old)):
        raise ValueError("Split numerator and denominator must be positive integers")
    if date.fromisoformat(effective_date) > date.fromisoformat(ex_date):
        raise ValueError("Split effective date cannot follow its adjusted trading date")
    date.fromisoformat(verified_at)
    if not conn.execute("SELECT 1 FROM accounts WHERE id=? AND account_type='investment'", (account_id,)).fetchone():
        raise ValueError("A corporate action must belong to an investment account")
    existing = conn.execute(
        "SELECT * FROM corporate_actions WHERE account_id=? AND isin=? AND action_type='split' AND ex_date=?",
        (account_id, isin, ex_date)).fetchone()
    if existing:
        if (existing["effective_date"] != effective_date or
                existing["ratio_new"] * ratio_old != ratio_new * existing["ratio_old"]):
            raise ValueError("Conflicting corporate action; review the existing sourced event")
        return existing["id"]
    return conn.execute(
        "INSERT INTO corporate_actions(account_id,isin,action_type,effective_date,ex_date,ratio_new,ratio_old,"
        "source_url,source_title,verified_at,notes) VALUES(?,?,'split',?,?,?,?,?,?,?,?)",
        (account_id, isin, effective_date, ex_date, ratio_new, ratio_old,
         source_url, source_title, verified_at, notes)).lastrowid


def sync_known_actions(conn) -> int:
    """Add reviewed issuer actions for securities actually present in each account.

    Called at schema migration and after imports, so fresh/repeated imports and
    existing installations follow the same path. It never modifies trade rows.
    """
    securities = set()
    for row in conn.execute("SELECT t.account_id,t.meta FROM transactions t JOIN accounts a ON a.id=t.account_id "
                            "WHERE a.account_type='investment' AND t.meta IS NOT NULL"):
        try:
            meta = json.loads(row["meta"])
        except (TypeError, ValueError):
            continue
        if isinstance(meta, dict) and meta.get("isin"):
            securities.add((row["account_id"], meta["isin"]))
    before = conn.total_changes
    for action in KNOWN_SPLITS:
        for account_id, isin in sorted(securities):
            if isin == action["isin"]:
                record_split(conn, account_id, **action)
    return conn.total_changes - before

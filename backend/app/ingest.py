"""Ingest bank statement PDFs into SQLite: dispatch to parsers, normalize
merchants, auto-categorize, and store with reconciliation status.

Two entry points:
  - ingest_dir(root)   folder-routed (Data/<Bank>/*.pdf) — the canonical build.
  - ingest_file(path)  bank auto-detected from the PDF — retained for authorized
    operator ingestion and recovery of saved source documents.
"""
from __future__ import annotations
import hashlib
import json
import math
import re
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pdfplumber

from .db import init_db, connect, DB_PATH
from .parsers import amex, hsbc_debit, hsbc_credit, lloyds, revolut, trading212, moomoo
from .categorize import normalize_merchant, auto_category

# Folder-name -> parser module (canonical folder-routed ingest).
SOURCES = {
    "AMEX": amex,
    "HSBC Debit": hsbc_debit,
    "HSBC Credit": hsbc_credit,
    "Lloyds": lloyds,
    "Revolut": revolut,
    "Trading212": trading212,
    "Moomoo": moomoo,
}

# Auto-detection: module -> the folder we file the statement under when uploaded.
FOLDER_FOR = {module: folder for folder, module in SOURCES.items()}

# Statement file extensions we accept (Trading212 is CSV; banks are PDF).
EXTS = ("*.pdf", "*.csv")


def detect_bank(path: str):
    """Sniff a statement file and return (folder, parser_module), or (None, None).
    Handles Trading212 CSVs (by header) and bank PDFs (by first-page text)."""
    # CSV / Trading212: read the first couple of KB as text.
    try:
        with open(path, "r", encoding="utf-8-sig", errors="ignore") as fh:
            head = fh.read(2048)
    except Exception:
        head = ""
    if "Action" in head and "Time" in head and "ISIN" in head and "Total" in head:
        return "Trading212", trading212
    if str(path).lower().endswith(".csv"):
        return (None, None)  # unrecognised CSV layout

    try:
        with pdfplumber.open(path) as pdf:
            text = pdf.pages[0].dedupe_chars().extract_text() or ""
    except Exception:
        return None, None
    low = text.lower()
    flat = re.sub(r"\s+", "", low)
    has = lambda *ks: any(k in flat for k in ks)

    if has("moomoo") or (has("monthlystatement") and has("marginaccount") and has("netassetvalue") and "securities" in low):
        return "Moomoo", moomoo
    if has("americanexpress") and has("membershipnumber"):
        return "AMEX", amex
    if has("revolut.com", "revolutltd", "revolutbank"):
        return "Revolut", revolut
    if has("lloydsbank"):
        return "Lloyds", lloyds
    if "hsbc" in low:
        if has("previousbalance", "newbalance"):
            return "HSBC Credit", hsbc_credit
        return "HSBC Debit", hsbc_debit
    # Structural fallbacks if the bank name didn't extract.
    if has("previousbalance", "newbalance"):
        return "HSBC Credit", hsbc_credit
    if has("paidin", "paidout") or has("balancebroughtforward"):
        return "HSBC Debit", hsbc_debit
    if has("moneyin", "moneyout"):
        return "Lloyds", lloyds
    return None, None


def reconciled(s) -> bool | None:
    """True / False / None: verified, failed, or no independent balances."""
    if (s.meta or {}).get("parser") == "moomoo":
        return (s.meta or {}).get("native_reconciled")
    return s.reconciles()


def _validate_statement(s) -> None:
    if not s.account_id or (not s.account_number and s.account_type != "investment"):
        raise ValueError("Statement account identity could not be read")
    if s.period_start and s.period_end and s.period_start > s.period_end:
        raise ValueError("Statement period starts after it ends")
    for t in s.txns:
        if t.txn_date is None or not t.description.strip() or not math.isfinite(t.amount):
            raise ValueError("Statement contains an invalid transaction date, description, or amount")
    for balance in (s.opening_balance, s.closing_balance):
        if balance is not None and not math.isfinite(balance):
            raise ValueError("Statement contains an invalid balance")


def _hash(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:20]


def _resolve_bank_identity(conn, statement):
    """Preserve existing IDs, separating conflicting full printed identities."""
    if statement.bank not in {'AMEX', 'HSBC'} or not statement.account_number:
        return
    existing = conn.execute('SELECT account_number,sort_code FROM accounts WHERE id=?', (statement.account_id,)).fetchone()
    if not existing:
        return
    canonical = lambda value: re.sub(r'[^0-9x]', '', str(value or '').lower())
    previous, incoming = canonical(existing['account_number']), canonical(statement.account_number)
    distinct = bool(previous.isdigit() and incoming.isdigit() and previous != incoming)
    distinct = distinct or (previous and incoming and len(previous) == len(incoming)
                and any(a.isdigit() and b.isdigit() and a != b for a, b in zip(previous, incoming)))
    distinct = distinct or bool(existing['sort_code'] and statement.sort_code and existing['sort_code'] != statement.sort_code)
    if not distinct:
        return
    if not incoming.isdigit():
        raise ValueError('Conflicting masked bank account identities require an independently identified statement.')
    identity = f'{statement.bank}|{statement.sort_code or ""}|{incoming}'
    statement.account_id += '-' + hashlib.sha256(identity.encode()).hexdigest()[:12]


def _statement_date_repairs(conn, statement, source, source_hash):
    """Reparse the same HSBC source without multiplying wrongly dated rows."""
    if statement.bank != 'HSBC' or source is None or not statement.reconciles():
        return {}
    old_hash = json.loads(source['meta'] or '{}').get('source_sha256')
    old_rows = list(conn.execute('SELECT * FROM transactions WHERE statement_id=? ORDER BY id', (source['id'],)))
    source_rows = [row for row in old_rows if json.loads(row['meta'] or '{}').get('source') != 'lunchflow']
    if not source_rows:
        return {}
    same = lambda old, new: (old['amount'] == new.amount and old['description'] == new.description
                            and old['raw'] == new.raw and old['type_code'] == new.type_code)
    complete_sequence = (len(source_rows) == len(statement.txns)
                         and all(same(old, new) for old, new in zip(source_rows, statement.txns)))
    dates_changed = any(same(old, new) and old['txn_date'] != new.txn_date.isoformat()
                        for old in source_rows for new in statement.txns)
    if not complete_sequence or (old_hash and old_hash != source_hash):
        if dates_changed:
            raise ValueError('Revised statement dates cannot be matched safely to the saved source. Review the original statement before importing.')
        return {}
    return dict(enumerate(source_rows))


def dedupe_key(account_id, date_s, amount, desc, occurrence, ext_id=None) -> str:
    """Stable per-transaction key.

    With a provider id (e.g. Trading212's ID) we key on that, so re-importing an
    overlapping export never double-counts. Otherwise we key on content plus an
    *occurrence index* — the Nth identical (date, amount, description) within the
    statement — which dedupes the same transaction appearing in two overlapping
    statements while still keeping genuine same-day same-amount repeats."""
    if ext_id:
        return _hash(f"{account_id}|id|{ext_id}")
    return _hash(f"{account_id}|{date_s}|{amount:.2f}|{desc}|{occurrence}")


def _broker_event_identity(meta):
    """No-ID dividends use execution time and native cash, not just a day."""
    if not meta.get('time') or not meta.get('action'):
        return None
    return (trading212.canonical_time(meta['time']), meta['action'],
            meta.get('isin'), meta.get('native_currency'), meta.get('native_amount'))


def _validate_broker_revision(existing, meta, amount):
    """Do not silently replace an identified event's financial facts."""
    from .corporate_actions import validate_trade_revision
    validate_trade_revision(existing['meta'], meta)
    old = json.loads(existing['meta'] or '{}')
    if not math.isclose(existing['amount'], amount, rel_tol=1e-10, abs_tol=1e-8):
        raise ValueError('An existing broker event has a changed cash amount. Review the conflicting export before importing it.')
    for field in ('action', 'isin', 'native_currency', 'native_amount', 'result_native',
                  'result_currency', 'execution_price', 'execution_price_currency', 'exchange_rate'):
        prior, current = old.get(field), meta.get(field)
        if prior is None or current is None:
            continue  # Enrich legacy rows without inventing missing evidence.
        same = math.isclose(prior, current, rel_tol=1e-10, abs_tol=1e-8) if isinstance(prior, (int,float)) and isinstance(current, (int,float)) else prior == current
        if not same:
            raise ValueError(f'An existing broker event has changed {field.replace("_", " ")}. Review the conflicting export before importing it.')
    if old.get('time') and meta.get('time') and trading212.canonical_time(old['time']) != trading212.canonical_time(meta['time']):
        raise ValueError('An existing broker event has a changed execution time. Review the conflicting export before importing it.')
    if old.get('cash_movements') is not None and old['cash_movements'] != meta.get('cash_movements'):
        raise ValueError('An existing broker event has changed currency cash legs. Review the conflicting export before importing it.')
    def fee_values(fees):
        return sorted((f['name'],f.get('currency'),float(f['amount'])) for f in (fees or []) if f['amount'])
    if old.get('fees') is not None and fee_values(old['fees']) != fee_values(meta.get('fees')):
        raise ValueError('An existing broker event has changed fees. Review the conflicting export before importing it.')


def _recover_hsbc_credit_periods(conn, account_id: str) -> None:
    """Recover monthly credit intervals only from independently checked sources.

    HSBC prints a statement date, previous/new balances and debit/credit totals,
    but no period start. An adjacent monthly statement with the same carried
    balance establishes the next period's start. Posting dates only corroborate
    it. Recompute on every import so a corrected/conflicting predecessor also
    revokes an earlier derivation, and imports work in either date order.
    """
    rows = conn.execute("SELECT * FROM statements WHERE account_id=? AND period_end IS NOT NULL "
                        "ORDER BY period_end,id", (account_id,)).fetchall()
    by_end = {}
    for row in rows:
        by_end.setdefault(row['period_end'], []).append(row)
    previous_group = None
    for end_s, group in by_end.items():
        for row in group:
            meta = json.loads(row['meta'] or '{}')
            derived = meta.pop('period_start_evidence', None)
            # Leave explicit intervals from a different source untouched.
            if row['period_start'] is not None and not derived:
                continue
            start = None
            prev = previous_group[0] if previous_group and len(previous_group) == 1 else None
            if len(group) == 1 and prev is not None:
                pm = json.loads(prev['meta'] or '{}')

                def verified_summary(record, evidence):
                    return (record['reconciled'] == 1
                            and record['opening_balance'] is not None
                            and record['closing_balance'] is not None
                            and evidence.get('parser') == 'hsbc_credit'
                            and evidence.get('printed_statement_date') == record['period_end']
                            and evidence.get('printed_debits') is not None
                            and evidence.get('printed_credits') is not None
                            and evidence.get('posting_dates_valid') is True
                            and (not evidence.get('last_posting_date')
                                 or evidence['last_posting_date'] <= record['period_end']))

                if verified_summary(row, meta) and verified_summary(prev, pm):
                    end = date.fromisoformat(end_s)
                    prev_end = date.fromisoformat(prev['period_end'])
                    adjacent_month = (end.year * 12 + end.month) - (prev_end.year * 12 + prev_end.month) == 1
                    same_balance = Decimal(str(row['opening_balance'])) == Decimal(str(prev['closing_balance']))
                    # Allow the observed monthly cycle's weekend drift, but do
                    # not bridge missing months, irregular cycles or overlaps.
                    same_card = meta.get('printed_card_number') and meta['printed_card_number'] == pm.get('printed_card_number')
                    if (adjacent_month and 27 <= (end - prev_end).days <= 33 and same_balance and same_card
                            and (not meta.get('first_posting_date')
                                 or meta['first_posting_date'] > prev['period_end'])):
                        start = (prev_end + timedelta(days=1)).isoformat()
                        meta['period_start_evidence'] = {
                            'method': 'reconciled_previous_statement',
                            'previous_statement_id': prev['id'],
                            'previous_period_end': prev['period_end'],
                            'previous_closing_balance': prev['closing_balance'],
                            'opening_balance': row['opening_balance'],
                        }
            if start != row['period_start'] or derived != meta.get('period_start_evidence'):
                conn.execute("UPDATE statements SET period_start=?,meta=? WHERE id=?",
                             (start, json.dumps(meta), row['id']))
        previous_group = group


def _store_statement(conn, s, pdf_path: str) -> dict:
    """Insert one ParsedStatement + its transactions. Idempotent (INSERT OR
    IGNORE on unique keys). Returns a per-statement result summary."""
    cur = conn.cursor()
    _validate_statement(s)
    _resolve_bank_identity(conn, s)
    observed_source_file = str(pdf_path)
    statement_meta = dict(s.meta or {})
    if Path(pdf_path).is_file():
        with open(pdf_path, 'rb') as source:
            statement_meta['source_sha256'] = hashlib.file_digest(source, 'sha256').hexdigest()
    if s.bank == 'Trading212':
        from .trading212_accounts import resolve
        resolve(conn, s, pdf_path, statement_meta.get('source_sha256'))
        statement_meta = {**statement_meta, **(s.meta or {})}
    existing_source = cur.execute("SELECT * FROM statements WHERE source_file=?",
                                  (str(pdf_path),)).fetchone()
    if existing_source and existing_source["account_id"] != s.account_id:
        raise ValueError("This source filename is already assigned to a different account")
    disp = (s.meta or {}).get("account_label") or f"{s.bank} {s.account_type.title()}"
    cur.execute(
        "INSERT OR IGNORE INTO accounts(id,bank,account_type,sort_code,account_number,display_name)"
        " VALUES(?,?,?,?,?,?)",
        (s.account_id, s.bank, s.account_type, s.sort_code, s.account_number, disp))
    rec = reconciled(s)
    # Repositories and Docker mounts can move. Reuse a same-account statement
    # with the same filename and end date instead of multiplying metadata rows.
    old_sources = cur.execute("SELECT id,source_file,period_start,period_end,meta FROM statements WHERE account_id=?",
                              (s.account_id,)).fetchall()
    start_s = s.period_start.isoformat() if s.period_start else None
    end_s = s.period_end.isoformat() if s.period_end else None
    if s.bank == 'Moomoo':
        # A replacement must not silently leave both the old and revised
        # economic events in the ledger. Same-content re-exports are harmless;
        # daily and monthly reports can share a closing day but cover different
        # intervals. Preserve both independently reconciled source documents.
        same_period = [row for row in old_sources
                       if row['period_start'] == start_s and row['period_end'] == end_s]
        if same_period:
            incoming = {dedupe_key(s.account_id, t.txn_date.isoformat(), t.amount, t.description, 0, ext_id=t.ext_id) for t in s.txns}
            statement_ids = {row['id'] for row in same_period}
            existing_events = conn.execute('SELECT dedupe_key,statement_id,meta FROM transactions WHERE account_id=?', (s.account_id,)).fetchall()
            existing_keys = {row['dedupe_key'] for row in existing_events
                             if row['statement_id'] in statement_ids or any(
                                 point.get('source_statement_start') == start_s and point.get('source_statement_end') == end_s
                                 for point in json.loads(row['meta'] or '{}').get('source_observations', []))}
            if existing_keys != incoming:
                raise ValueError('A Moomoo monthly statement has changed events. Review the conflicting statement before importing; existing records were preserved.')
            for row in same_period:
                prior_meta = json.loads(row['meta'] or '{}')
                fields = ('cash_balances', 'opening_cash_balances', 'net_asset_value', 'portfolio_value')
                prior_positions = [(p.get('isin'), p.get('shares'), p.get('price'), p.get('currency')) for p in prior_meta.get('positions', [])]
                new_positions = [(p.get('isin'), p.get('shares'), p.get('price'), p.get('currency')) for p in statement_meta.get('positions', [])]
                if (any(prior_meta.get(field) != statement_meta.get(field) for field in fields)
                        or (prior_meta.get('opening_net_asset_value') is not None
                            and statement_meta.get('opening_net_asset_value') is not None
                            and prior_meta['opening_net_asset_value'] != statement_meta['opening_net_asset_value'])
                        or sorted(prior_positions) != sorted(new_positions)):
                    raise ValueError('A Moomoo monthly statement has changed its cash or position snapshot. Review the conflicting statement before importing.')
            if (not existing_source and len(same_period) == 1
                    and json.loads(same_period[0]['meta'] or '{}').get('source_sha256') == statement_meta.get('source_sha256')):
                pdf_path = same_period[0]['source_file']
    # Uploads use content-addressed filenames. Reuse an already imported exact
    # source even when its name differs, preserving one statement/evidence row.
    source_hash = statement_meta.get('source_sha256')
    identical = [row for row in old_sources if source_hash and row['period_end'] == end_s
                 and json.loads(row['meta'] or '{}').get('source_sha256') == source_hash]
    if not existing_source and len(identical) == 1:
        pdf_path = identical[0]['source_file']
    relocated = [row for row in old_sources if Path(row["source_file"]).name == Path(pdf_path).name
                 and row["period_end"] == end_s and row["source_file"] != str(pdf_path)]
    if s.bank in ('Trading212', 'Moomoo') and len(relocated) == 1:
        # Different PDF bytes are a separate source, even when they describe
        # the same events. Never pair one source path with another file's hash.
        if (s.bank != 'Moomoo'
                or json.loads(relocated[0]['meta'] or '{}').get('source_sha256') == source_hash):
            pdf_path = relocated[0]['source_file']
        relocated = []
    if len(relocated) == 1 and not cur.execute("SELECT 1 FROM statements WHERE source_file=?", (str(pdf_path),)).fetchone():
        cur.execute("UPDATE statements SET source_file=? WHERE id=?", (str(pdf_path), relocated[0]["id"]))
    repair_source = cur.execute('SELECT * FROM statements WHERE source_file=?', (str(pdf_path),)).fetchone()
    date_repairs = _statement_date_repairs(conn, s, repair_source, source_hash)
    cur.execute(
        "INSERT INTO statements(account_id,source_file,period_start,period_end,"
        "opening_balance,closing_balance,reconciled,meta) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(source_file) DO UPDATE SET period_start=excluded.period_start,"
        "period_end=excluded.period_end,opening_balance=excluded.opening_balance,"
        "closing_balance=excluded.closing_balance,reconciled=excluded.reconciled,meta=excluded.meta",
        (s.account_id, str(pdf_path),
         s.period_start.isoformat() if s.period_start else None,
         s.period_end.isoformat() if s.period_end else None,
         s.opening_balance, s.closing_balance, None if rec is None else int(rec),
         json.dumps(statement_meta) if statement_meta else None))
    stmt_id = cur.execute("SELECT id FROM statements WHERE source_file=?",
                          (str(pdf_path),)).fetchone()[0]
    added = 0
    occ: Counter = Counter()
    broker_occ: Counter = Counter()
    used_ids: set[int] = set()
    for source_index, t in enumerate(s.txns):
        ds = t.txn_date.isoformat() if t.txn_date else None
        merch = t.merchant or normalize_merchant(t.description)
        cat = t.category or auto_category(t.description, merch, t.amount)
        meta = dict(t.meta or {})
        meta["source_merchant"] = merch
        if t.category:
            meta["source_category"] = t.category
        sig = (ds, round(t.amount, 2), t.description)
        n = occ[sig]
        occ[sig] += 1
        key = dedupe_key(s.account_id, ds, t.amount, t.description, n, ext_id=t.ext_id)
        if source_index in date_repairs:
            repaired = date_repairs[source_index]
            key = repaired['dedupe_key']
            prior_meta = json.loads(repaired['meta'] or '{}')
            corrections = prior_meta.get('date_corrections', [])
            if repaired['txn_date'] != ds:
                corrections.append({'previous_date': repaired['txn_date'], 'corrected_date': ds,
                    'previous_raw': repaired['raw'], 'statement_id': stmt_id,
                    'source_sha256': statement_meta.get('source_sha256'), 'basis': 'reparsed_same_statement'})
            if corrections:
                meta['date_corrections'] = corrections
        broker = s.bank in ('Trading212', 'Moomoo') and s.account_type == 'investment'
        if broker and not t.ext_id and (identity := _broker_event_identity(meta)):
            event_occurrence = broker_occ[identity]
            broker_occ[identity] += 1
            # Reuse legacy IDs/keys when full event evidence agrees. Provider-ID
            # rows are never candidates for this timestamp-based reconciliation.
            possible = cur.execute('SELECT * FROM transactions WHERE account_id=? AND txn_date=? AND type_code=? ORDER BY id',
                                   (s.account_id,ds,t.type_code)).fetchall()
            matches = []
            for row in possible:
                old_meta = json.loads(row['meta'] or '{}')
                if row['id'] not in used_ids and not old_meta.get('provider_id') and _broker_event_identity(old_meta) == identity:
                    # Old parsers did not retain provider_id. Their ordinary
                    # content key proves these were also imported without one.
                    legacy_keys = {dedupe_key(s.account_id,ds,row['amount'],row['description'],i) for i in range(len(possible)+1)}
                    if 'provider_id' in old_meta or row['dedupe_key'] in legacy_keys:
                        matches.append(row)
            if matches:
                key = matches[0]['dedupe_key']
            else:
                key = dedupe_key(s.account_id,ds,t.amount,t.description,event_occurrence,
                                 ext_id='no-id-event|' + json.dumps(identity) + f'|{event_occurrence}')
        # Legacy databases used different keys and HSBC page headers leaked into
        # descriptions. Match each occurrence once, including overlapping export
        # periods, without losing IDs or user overrides. Provider IDs remain
        # authoritative and are never collapsed by same-amount content matching.
        existing = cur.execute("SELECT * FROM transactions WHERE dedupe_key=?", (key,)).fetchone()
        if not existing and not broker and s.account_type != 'investment':
            from .lunchflow_store import match_statement_transaction
            feed_row = match_statement_transaction(conn, s.account_id, ds, t.amount,
                                                   t.description, merch, used_ids,
                                                   posting_date=(meta.get('posting_date') or meta.get('posted_date')) if rec else None)
            if feed_row:
                # A statement arriving after an API fetch is additional source
                # evidence for the same row. Preserve its identity and reviews.
                feed_meta = json.loads(feed_row['meta'] or '{}')
                evidence = feed_meta.setdefault('statement_observations', [])
                item = {'statement_id': stmt_id, 'date': ds, 'description': t.description,
                        'amount': t.amount, 'balance': t.balance, 'raw': t.raw}
                if item not in evidence:
                    evidence.append(item)
                chosen_date = feed_row['txn_date']
                if rec:
                    # A feed can use settlement day while the PDF prints the
                    # purchase day. Monthly reporting must not depend on which
                    # source arrived first. Raw API evidence remains untouched.
                    chosen_date = ds
                    feed_meta.setdefault('provider_transaction_date', feed_row['txn_date'])
                    feed_meta['statement_transaction_date'] = ds
                    posted = meta.get('posting_date') or meta.get('posted_date')
                    if posted:
                        feed_meta['posting_date'] = posted
                cur.execute('UPDATE transactions SET statement_id=COALESCE(statement_id,?),txn_date=?,meta=? WHERE id=?',
                            (stmt_id, chosen_date, json.dumps(feed_meta), feed_row['id']))
                used_ids.add(feed_row['id'])
                continue
        if existing:
            old_metadata = json.loads(existing['meta'] or '{}')
            if 'date_corrections' in old_metadata and 'date_corrections' not in meta:
                meta['date_corrections'] = old_metadata['date_corrections']
            if broker:
                _validate_broker_revision(existing, meta, t.amount)
            elif s.account_type == 'investment':
                from .corporate_actions import validate_trade_revision
                validate_trade_revision(existing['meta'],meta)
            used_ids.add(existing["id"])
        elif not t.ext_id and not broker:
            candidates = cur.execute(
                "SELECT id,description,dedupe_key,balance,meta FROM transactions WHERE account_id=? "
                "AND txn_date=? AND amount=? ORDER BY CASE WHEN statement_id=? THEN 0 ELSE 1 END,id",
                (s.account_id, ds, t.amount, stmt_id)).fetchall()
            legacy = [row for row in candidates if row["id"] not in used_ids
                      and (row["description"] == t.description
                           or (row["description"] or "").endswith(' ' + t.description)
                           # Revolut can rename old merchant labels in a new
                           # export; the same running balance anchors the row.
                           or (t.balance is not None and row["balance"] == t.balance))]
            if legacy:
                old = legacy[0]
                old_meta = json.loads(old['meta'] or '{}')
                if 'date_corrections' in old_meta:
                    meta['date_corrections'] = old_meta['date_corrections']
                used_ids.add(old["id"])
                cur.execute("INSERT OR IGNORE INTO overrides(dedupe_key,category,merchant,note,merchant_kind,spending_treatment,exclude_from_analysis) "
                            "SELECT ?,category,merchant,note,merchant_kind,spending_treatment,exclude_from_analysis FROM overrides WHERE dedupe_key=?",
                            (key, old["dedupe_key"]))
                cur.execute("DELETE FROM overrides WHERE dedupe_key=?", (old["dedupe_key"],))
                cur.execute("UPDATE transactions SET dedupe_key=? WHERE id=?", (key, old["id"]))
        if broker:
            old_meta = json.loads(existing['meta'] or '{}') if existing else {}
            observations = old_meta.get('source_observations', [])
            if s.bank == 'Moomoo':
                # Give a newly imported monthly event its interval on the first
                # pass. Otherwise re-importing the same PDF changes its metadata.
                meta.setdefault('source_statement_start', start_s)
            if s.bank == 'Moomoo' and existing:
                # Legacy evidence lacked interval fields. Recover the immutable
                # first statement's scope before adding an overlapping report,
                # so later monthly/daily imports cannot steal replay membership.
                original = cur.execute('SELECT source_file,period_start,period_end,meta FROM statements WHERE id=?',
                                       (existing['statement_id'],)).fetchone()
                if original:
                    original_hash = json.loads(original['meta'] or '{}').get('source_sha256')
                    matched = [point for point in observations if point.get('source_file') == original['source_file']
                               or (original_hash and point.get('source_sha256') == original_hash)]
                    if not matched:
                        matched = [{'source_file': original['source_file'], 'source_sha256': original_hash}]
                        observations.extend(matched)
                    for point in matched:
                        point.setdefault('source_statement_start', original['period_start'])
                        point.setdefault('source_statement_end', original['period_end'])
                if old_meta.get('source_statement_end'):
                    meta['source_statement_end'] = old_meta['source_statement_end']
                    if old_meta.get('source_statement_start'):
                        meta['source_statement_start'] = old_meta['source_statement_start']
                    elif original and original['period_end'] == old_meta['source_statement_end']:
                        meta['source_statement_start'] = original['period_start']
                    else:
                        meta.pop('source_statement_start', None)
                if old_meta.get('source_legs'):
                    if meta.get('source_legs'):
                        economic = lambda legs: [tuple(leg.get(key) for key in (
                            'time', 'currency', 'native', 'direction', 'from_currency',
                            'to_currency', 'rate')) for leg in legs]
                        if economic(old_meta['source_legs']) != economic(meta['source_legs']):
                            raise ValueError('An existing Moomoo exchange has changed cash leg evidence.')
                    # The first report's page coordinates stay attached to its
                    # original event; the new source is linked below.
                    meta['source_legs'] = old_meta['source_legs']
            observation = {'source_file': observed_source_file, 'source_sha256': source_hash,
                           'source_line': meta.get('source_line'), 'source_row': meta.get('source_row')}
            if s.bank == 'Moomoo':
                observation.update(source_statement_start=start_s, source_statement_end=end_s)
            if meta.get('source_page') is not None:
                observation['source_page'] = meta['source_page']
            if observation not in observations:
                observations.append(observation)
            meta['source_observations'] = observations
            # The first statement linkage and raw source record are immutable;
            # newer exports enrich metadata and append inspectable observations.
            raw = existing['raw'] if existing else t.raw
        else:
            raw = t.raw
        cur.execute(
            "INSERT OR IGNORE INTO transactions(account_id,statement_id,txn_date,description,"
            "merchant,amount,balance,type_code,category,raw,meta,dedupe_key)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (s.account_id, stmt_id, ds, t.description, merch, t.amount, t.balance,
             t.type_code, cat, raw, json.dumps(meta) if meta else None, key))
        added += cur.rowcount
        used_ids.add(cur.execute("SELECT id FROM transactions WHERE dedupe_key=?", (key,)).fetchone()["id"])
        # Parser fixes must reach existing databases. Preserve user-edited
        # categories/merchants; the categorization pass reapplies saved rules.
        cur.execute(
            "UPDATE transactions SET txn_date=?,description=?,amount=?,balance=?,type_code=?,raw=?,meta=?,"
            "merchant=CASE WHEN category_source='user' THEN merchant ELSE ? END,"
            "category=CASE WHEN category_source IN ('user','rule') THEN category ELSE ? END "
            "WHERE dedupe_key=?",
            (ds, t.description, t.amount, t.balance, t.type_code, raw,
             json.dumps(meta) if meta else None, merch, cat, key))
    if s.bank == 'HSBC' and s.account_type == 'credit':
        _recover_hsbc_credit_periods(conn, s.account_id)
    if s.account_type == 'investment':
        from .corporate_actions import sync_known_actions
        sync_known_actions(conn)
    stored = cur.execute("SELECT period_start,meta FROM statements WHERE id=?", (stmt_id,)).fetchone()
    return {"bank": s.bank, "account": disp, "account_id": s.account_id,
            "n_txns": len(s.txns), "new_txns": added, "reconciled": rec,
            "period": [stored['period_start'], s.period_end.isoformat() if s.period_end else None],
            "period_start_evidence": json.loads(stored['meta'] or '{}').get('period_start_evidence')}


def ingest_dir(data_root: str, db_path: Path = DB_PATH) -> dict:
    conn = init_db(db_path)
    stats = {"statements": 0, "transactions": 0, "new_transactions": 0,
             "reconciled": 0, "failed": 0, "unverified": 0, "accounts": set()}
    root = Path(data_root)
    try:
        for folder, mod in SOURCES.items():
            files = sorted(f for f in (root / folder).glob("*") if f.suffix.lower() in (".pdf", ".csv"))
            if folder == 'Trading212':
                files += sorted(f for kind in ('Stocks', 'ISA') for f in (root / folder / kind).glob('*.csv'))
            for pdf in files:
                if mod is trading212:
                    from .trading212_accounts import folder_kind
                    s = mod.parse(str(pdf), account_kind=folder_kind(pdf))
                else:
                    s = mod.parse(str(pdf))
                r = _store_statement(conn, s, pdf)
                stats["statements"] += 1
                stats["transactions"] += r["n_txns"]
                stats["new_transactions"] += r["new_txns"]
                stats["reconciled"] += int(r["reconciled"] is True)
                stats["failed"] += int(r["reconciled"] is False)
                stats["unverified"] += int(r["reconciled"] is None)
                stats["accounts"].add(s.account_id)
        conn.commit()
        stats["accounts"] = len(stats["accounts"])
        return stats
    finally:
        conn.close()


def ingest_file(pdf_path: str, db_path: Path = DB_PATH, conn=None, trading212_account=None) -> dict:
    """Auto-detect the bank and ingest a single PDF. Returns a result summary;
    raises ValueError if the bank can't be recognised or the parse fails."""
    folder, mod = detect_bank(pdf_path)
    if mod is None:
        raise ValueError("Unrecognised statement format")
    own = conn is None
    if own:
        conn = init_db(db_path)
    try:
        if mod is trading212:
            from .trading212_accounts import folder_kind
            selected = trading212_account or folder_kind(pdf_path)
            s = mod.parse(str(pdf_path), account_kind=selected)
        else:
            s = mod.parse(str(pdf_path))
        if not s.txns and s.opening_balance is None and not (s.period_start and s.period_end):
            raise ValueError("No transactions found — is this a statement PDF?")
        if not conn.in_transaction:
            conn.execute("BEGIN")
        conn.execute("SAVEPOINT ingest_statement")
        try:
            res = _store_statement(conn, s, pdf_path)
        except Exception:
            conn.execute("ROLLBACK TO ingest_statement")
            conn.execute("RELEASE ingest_statement")
            raise
        conn.execute("RELEASE ingest_statement")
        res["folder"] = folder
        if own:
            conn.commit()
        return res
    finally:
        if own:
            conn.close()


def ingest_uploads(uploads_root: str, db_path: Path = DB_PATH) -> dict:
    """Auto-detect-ingest every PDF under an uploads folder (any nesting). Used by
    build_db so user-added statements survive a full rebuild."""
    root = Path(uploads_root)
    if not root.exists():
        return {"files": 0, "ingested": 0}
    conn = connect(db_path)
    files = ingested = 0
    try:
        candidates = sorted(f for f in root.rglob("*") if f.suffix.lower() in (".pdf", ".csv"))
        for pdf in candidates:
            files += 1
            try:
                ingest_file(str(pdf), conn=conn)
                ingested += 1
            except Exception as e:  # noqa: BLE001 — keep going on a bad file
                print(f"[ingest_uploads] skip {pdf.name}: {e}", flush=True)
        conn.commit()
    finally:
        conn.close()
    return {"files": files, "ingested": ingested}


if __name__ == "__main__":
    import sys, json
    root = sys.argv[1] if len(sys.argv) > 1 else "../../Data"
    print(json.dumps(ingest_dir(root), indent=2))

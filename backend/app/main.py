"""FastAPI backend for the finance app.

Reads the populated SQLite DB (data/finance.db) and exposes JSON endpoints for
the dashboard: account summary, filtered/sorted/paginated transactions, time
series for charting, the local-intelligence outputs (recurring, transfers,
anomalies, advice), plus category-override / rule / recompute writes.

All money is signed: + money in, - money out. Internal cross-account transfers
(transfer_group IS NOT NULL) are excluded from income/spending KPIs so moving
your own money between banks doesn't inflate the totals.
"""
from __future__ import annotations
import re
import json
from datetime import date
import sqlite3
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Optional, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .security import RequestSizeMiddleware, SecurityHeadersMiddleware, validate_deployment
from .auth import AuthenticationMiddleware, router as auth_router
from . import analytics, planning, merchants, merchant_inbox, recurring_views
from . import banking_insights
from .analytics import CASHFLOW
from .db import DB_PATH, connect, init_db, apply_startup_migrations
from .enrichment import router as enrichment_router
from .snaptrade_routes import router as snaptrade_router
from .snaptrade_store import seed_activity_archive
from .wealth_routes import router as wealth_router
from .investing_routes import router as investing_router
from .market_routes import router as market_router
from .merchant_merge import router as merchant_merge_router
from .portfolio_scheduler import portfolio_scheduler
from .snapshot_retention import snapshot_retention
from .market_price_scheduler import market_price_scheduler
from .wealth import scheduled_fx_refresh
from .lunchflow_routes import router as banking_router
from .lunchflow_scheduler import banking_scheduler
from .lunchflow_store import ensure_schema as ensure_banking_schema
from .runtime import RequestBoundaryMiddleware, database_readiness, database_revision
from .banking_monitor import BankingTimingMiddleware
from .categorize import normalize_merchant, apply_categorization
from .portfolio_audit import trade_history
from .workspace import router as workspace_router, category_options as workspace_category_options, category_name
from .account_settings import router as account_settings_router
from .account_coverage import router as account_coverage_router
from .product_imports import router as product_imports_router
from .currency_reporting import router as currency_reporting_router
from .setup import router as setup_router
from .product_connections import router as connections_router, saved_connections_scheduler
from .provider_oauth import router as provider_authorization_router, callback_router

@asynccontextmanager
async def lifespan(app):
    validate_deployment()
    # Apply additive schema updates before serving an existing installation.
    # A missing database requires explicit new-install initialization or recovery;
    # startup never substitutes an empty ledger for an unexpectedly lost volume.
    if DB_PATH.exists():
        # An empty/unrelated SQLite file is also a configuration/recovery error.
        # Check only core tables so legitimate older ledger schemas can migrate.
        with closing(sqlite3.connect(DB_PATH.resolve().as_uri() + '?mode=ro', uri=True)) as existing:
            tables = {row[0] for row in existing.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'accounts', 'transactions', 'statements'} <= tables:
                raise RuntimeError('The existing database is not an initialized financial ledger. Restore its backup or explicitly initialize a new installation in empty storage.')
        conn = init_db(DB_PATH)
        try:
            ensure_banking_schema(conn)
            conn.commit()
            apply_startup_migrations(conn)
        finally:
            conn.close()
        seed_activity_archive(DB_PATH)
    async with portfolio_scheduler(after_refresh=scheduled_fx_refresh), banking_scheduler(after_refresh=scheduled_fx_refresh), market_price_scheduler(), snapshot_retention(), saved_connections_scheduler(after_refresh=scheduled_fx_refresh):
        yield


app = FastAPI(title="Am I Broke? API", version="1.0", lifespan=lifespan)
app.include_router(enrichment_router)
app.include_router(auth_router)
app.include_router(snaptrade_router)
app.include_router(wealth_router)
app.include_router(banking_router)
app.include_router(investing_router)
app.include_router(market_router)
app.include_router(merchant_merge_router)
app.include_router(workspace_router)
app.include_router(account_settings_router)
app.include_router(account_coverage_router)
app.include_router(product_imports_router)
app.include_router(currency_reporting_router)
app.include_router(setup_router)
app.include_router(connections_router)
app.include_router(provider_authorization_router)
app.include_router(callback_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestSizeMiddleware)
app.add_middleware(BankingTimingMiddleware)
app.add_middleware(AuthenticationMiddleware)
app.add_middleware(RequestBoundaryMiddleware)
app.add_middleware(SecurityHeadersMiddleware)


@app.exception_handler(RequestValidationError)
async def validation_error(request, error):
    # FastAPI normally echoes rejected input, which may include a password.
    return JSONResponse({'detail': [{'loc': item['loc'], 'msg': item['msg'], 'type': item['type']}
                                   for item in error.errors()]}, status_code=422)


def get_conn() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise HTTPException(503, "Database is missing. Restore your Am I Broke? backup, or import statements for a new installation. See docs/DEPLOYMENT.md.")
    return connect(DB_PATH)


def rows(conn, sql, params=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params)]


# ----- summary / KPIs ----------------------------------------------------------

@app.get("/api/summary")
def summary():
    conn = get_conn()
    try:
        snapshots = {a['id']: a for a in planning.account_snapshot(conn)}
        accounts = rows(conn, """SELECT a.id,a.bank,a.account_type,a.display_name,
            (SELECT COUNT(*) FROM transactions t WHERE t.account_id=a.id) AS n_txns
            FROM accounts a ORDER BY a.bank,a.account_type""")
        for a in accounts:
            snap = snapshots[a['id']]
            a.update(balance=snap['balance'], balance_asof=snap['balance_asof'],
                     native_balance=snap['native_balance'], currency=snap['currency'],
                     balance_source=snap['balance_source'], balance_fetched_at=snap['balance_fetched_at'],
                     balance_stale=snap['balance_stale'],
                     reconciled=snap['reconciled'], sort_code=None, account_number=None)
        flows = analytics.monthly_cashflow(conn)
        agg = {'income': sum(p['income'] for p in flows), 'spending': -sum(p['spend'] for p in flows)}
        meta = conn.execute(
            "SELECT COUNT(*) AS n, MIN(txn_date) AS first_date, MAX(txn_date) AS last_date"
            " FROM transactions").fetchone()
        cash_total = sum((a["balance"] or 0) for a in accounts if snapshots[a['id']].get('include_in_reports', True))
        # Holdings market value from cached prices (no network call here).
        hv = analytics.household_holdings_value(conn)
        cash_total += hv.get("foreign_cash_value", 0)
        net_worth = cash_total + hv["market_value"]
        return {
            "accounts": accounts,
            "net_worth": round(net_worth, 2),
            "cash_total": round(cash_total, 2),
            "foreign_cash_value": hv.get("foreign_cash_value", 0),
            "holdings_value": hv["market_value"],
            "unrealised_pl": hv["unrealised"],
            "realised_pl": hv["realised"],
            "estimated_unrealised_pl": hv.get("estimated_unrealised"),
            "estimated_realised_pl": hv.get("estimated_realised"),
            "prices_asof": hv["asof"],
            "holdings_unpriced": hv["n_unpriced"],
            "holdings_stale": hv.get("n_stale", 0),
            "foreign_cash_unvalued": hv.get("foreign_cash_unvalued", 0),
            "wealth_partial": bool(hv["n_unpriced"] or hv.get("n_stale", 0) or hv.get("foreign_cash_unvalued", 0) or hv.get("foreign_cash_stale", 0) or not hv.get("position_complete", True) or any(a["balance"] is None or a["reconciled"] is not True or snapshots[a["id"]]["age_days"] is None or snapshots[a["id"]]["age_days"] > 45 for a in accounts if snapshots[a['id']].get('include_in_reports', True))),
            "income": round(agg["income"], 2),
            "spending": round(agg["spending"], 2),
            "net": round(agg["income"] + agg["spending"], 2),
            "n_transactions": meta["n"],
            "date_range": [meta["first_date"], meta["last_date"]],
            "recurring_count": conn.execute("SELECT COUNT(*) FROM recurring").fetchone()[0],
            "anomaly_count": conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE is_anomaly = 1").fetchone()[0],
            "transfer_count": conn.execute(
                "SELECT COUNT(DISTINCT transfer_group) FROM transactions"
                " WHERE transfer_group IS NOT NULL").fetchone()[0],
        }
    finally:
        conn.close()


# ----- transactions ------------------------------------------------------------

SORT_COLS = {"date": "txn_date", "amount": "amount", "merchant": "merchant",
             "description": "description", "category": "category"}


@app.get("/api/transactions")
def transactions(
    q: Optional[str] = None,
    account: Optional[str] = None,
    category: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    min_amount: Optional[float] = None,
    max_amount: Optional[float] = None,
    direction: Optional[str] = Query(None, pattern="^(in|out)$"),
    anomaly: Optional[bool] = None,
    recurring: Optional[bool] = None,
    transfer: Optional[bool] = None,
    sort: str = Query("date", pattern="^(date|amount|merchant|description|category)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(100, ge=1, le=2000),
    offset: int = Query(0, ge=0),
):
    conn = get_conn()
    try:
        # All filter columns are qualified with the `t` alias so the same WHERE
        # clause is safe in the unjoined count/sum queries and the joined list.
        where, params = ["COALESCE(t.currency,'GBP')='GBP'"], []
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "Start date must not follow end date")
        if min_amount is not None and max_amount is not None and min_amount > max_amount:
            raise HTTPException(422, "Minimum amount must not exceed maximum")
        if q:
            where.append("(t.description LIKE ? OR t.merchant LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        if account:
            where.append("t.account_id = ?"); params.append(account)
        if category:
            where.append("t.category = ?"); params.append(category)
            params[-1] = category_name(conn, category)
        if date_from:
            where.append("t.txn_date >= ?"); params.append(date_from.isoformat())
        if date_to:
            where.append("t.txn_date <= ?"); params.append(date_to.isoformat())
        if min_amount is not None:
            where.append("t.amount >= ?"); params.append(min_amount)
        if max_amount is not None:
            where.append("t.amount <= ?"); params.append(max_amount)
        if direction == "in":
            where.append("t.amount > 0")
        elif direction == "out":
            where.append("t.amount < 0")
        if anomaly is not None:
            where.append("t.is_anomaly = ?"); params.append(int(anomaly))
        if recurring is not None:
            where.append("t.recurring_id IS " + ("NOT NULL" if recurring else "NULL"))
        if transfer is not None:
            transfer_clause = "(t.transfer_group IS NOT NULL OR COALESCE(t.spending_treatment,'normal')='self_transfer')"
            where.append(transfer_clause if transfer else "NOT " + transfer_clause)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute(
            f"SELECT COUNT(*) FROM transactions t{clause}", params).fetchone()[0]
        sum_in, sum_out = conn.execute(
            f"SELECT COALESCE(SUM(CASE WHEN t.amount>0 THEN t.amount END),0),"
            f" COALESCE(SUM(CASE WHEN t.amount<0 THEN t.amount END),0)"
            f" FROM transactions t{clause}", params).fetchone()
        col = SORT_COLS[sort]
        items = rows(conn, f"""
            SELECT t.id, t.account_id, a.display_name AS account, t.txn_date, t.description,
                   t.merchant, t.amount, t.balance, t.category, t.category_source,
                   t.recurring_id, t.transfer_group, t.is_anomaly, t.source_merchant, t.merchant_kind,
                   t.spending_treatment, t.exclude_from_analysis, t.classification
            FROM transactions t JOIN accounts a ON a.id = t.account_id
            {clause}
            ORDER BY t.{col} {order.upper()}, t.id {order.upper()}
            LIMIT ? OFFSET ?
        """, params + [limit, offset])
        return {"total": total, "limit": limit, "offset": offset,
                "sum_in": round(sum_in, 2), "sum_out": round(sum_out, 2), "items": [merchants.public_transaction(t) for t in items]}
    finally:
        conn.close()


# ----- time series -------------------------------------------------------------

@app.get("/api/timeseries")
def timeseries(interval: str = Query("month", pattern="^(day|month)$"),
               account: Optional[str] = None):
    conn = get_conn()
    try:
        fmt = "%Y-%m" if interval == "month" else "%Y-%m-%d"
        net = []
        cumulative = 0.0
        for p in analytics.monthly_cashflow(conn, account=account, interval=interval):
            cumulative += p['net']
            net.append({'period': p['m'], 'income': p['income'], 'spending': -p['spend'],
                        'net': p['net'], 'cumulative': round(cumulative, 2)})
        # Use statement balances as anchors. Summing through missing statement
        # periods without resetting to observed balances fabricates history.
        balances = {}
        excluded_balance_accounts = 0
        for a in rows(conn, "SELECT id,display_name,account_type,currency FROM accounts ORDER BY display_name"):
            if account and a['id'] != account:
                continue
            # Investment cash movements are not portfolio value. Native foreign
            # account balances cannot share this GBP axis without dated FX.
            if a['account_type'] == 'investment' or a['currency'] not in (None, 'GBP'):
                excluded_balance_accounts += 1
                continue
            sign = -1 if a['account_type'] == 'credit' else 1
            statements = rows(conn, "SELECT * FROM statements WHERE account_id=? AND period_end IS NOT NULL ORDER BY period_end,id", (a['id'],))
            events = []
            for t in rows(conn, "SELECT id,txn_date,amount,balance FROM transactions WHERE account_id=? AND txn_date IS NOT NULL ORDER BY txn_date,id", (a['id'],)):
                events.append((t['txn_date'], 1, t['id'], 'delta', t['amount']))
            for st in statements:
                if st['closing_balance'] is not None and a['account_type'] != 'investment':
                    events.append((st['period_end'], 2, st['id'], 'anchor', sign*st['closing_balance']))
                if st['opening_balance'] is not None and st['period_start'] and a['account_type'] != 'investment':
                    events.append((st['period_start'], 0, st['id'], 'anchor', sign*st['opening_balance']))
            running = 0.0 if a['account_type'] == 'investment' else None
            points = {}
            for d, _, __, kind, value in sorted(events):
                if kind == 'anchor':
                    running = value
                elif running is not None:
                    running += value
                if running is not None:
                    period = d[:7] if interval == 'month' else d
                    points[period] = round(running, 2)
            if points:
                balances[a['display_name']] = [{'period': k, 'balance': v} for k,v in points.items()]
        return {"interval": interval, "net": net, "balances": balances,
                "balances_currency": "GBP", "excluded_balance_accounts": excluded_balance_accounts}
    finally:
        conn.close()


# ----- categories / intelligence ----------------------------------------------

@app.get("/api/categories")
def categories():
    conn = get_conn()
    try:
        totals = {}
        for t in analytics.cashflow_rows(conn):
            category = t['category'] or 'Uncategorized'
            p = totals.setdefault(category, {'category': category, 'n': 0, 'spent': 0.0, 'received': 0.0})
            p['n'] += 1
            p['spent'] += -t['amount'] if t['amount'] < 0 or t['is_refund'] else 0
            p['received'] += t['amount'] if t['amount'] > 0 and not t['is_refund'] else 0
        for p in totals.values():
            p['spent'], p['received'] = round(p['spent'], 2), round(p['received'], 2)
        return sorted(totals.values(), key=lambda p: p['spent'], reverse=True)
    finally:
        conn.close()


@app.get("/api/category-options")
def category_options():
    """Editable category names, independent of household-spending exclusions."""
    conn = get_conn()
    try:
        configured = workspace_category_options(conn)
        if configured is not None:
            # Preserve the original alphabetic API contract. The product
            # endpoint additionally supports explicit category ordering.
            return sorted(configured, key=lambda value: (value.casefold(), value))
        return [row['category'] for row in conn.execute("""
            SELECT category FROM transactions WHERE TRIM(COALESCE(category,'')) != ''
            UNION
            SELECT category FROM merchants WHERE enabled=1 AND TRIM(COALESCE(category,'')) != ''
            ORDER BY category COLLATE NOCASE, category
        """)]
    finally:
        conn.close()


@app.get("/api/recurring")
def recurring(
    details: bool = False,
    min_occurrences: int = Query(3, ge=2, le=24),
    amount_tol: float = Query(0.10, ge=0.01, le=1.0),
    regularity: float = Query(0.5, ge=0.0, le=1.0),
    tol_weekly: int = Query(2, ge=0, le=6),
    tol_fortnightly: int = Query(2, ge=0, le=7),
    tol_monthly: int = Query(5, ge=0, le=14),
    tol_bimonthly: int = Query(3, ge=0, le=20),
    tol_quarterly: int = Query(5, ge=0, le=30),
    tol_annual: int = Query(15, ge=0, le=60),
):
    day_tol = {"weekly": tol_weekly, "fortnightly": tol_fortnightly, "monthly": tol_monthly,
               "bi-monthly": tol_bimonthly, "quarterly": tol_quarterly, "annual": tol_annual}
    conn = get_conn()
    try:
        conn.execute('BEGIN')
        source_rows = analytics._rows(conn)
        spending = [component for row in source_rows
                    if (component := analytics._household_component(row)) is not None and component['amount'] < 0]
        patterns = analytics.detect_recurring(
            conn, min_occurrences=min_occurrences, amount_tol=amount_tol,
            day_tol=day_tol, regularity=regularity, source_rows=source_rows, spending_rows=spending)
        return recurring_views.overview(conn, patterns, spending_rows=spending) if details else patterns
    finally:
        conn.close()


@app.get("/api/transfers")
def transfers(window_days: int = Query(3, ge=0, le=30), require_name: bool = False):
    conn = get_conn()
    try:
        groups = analytics.detect_transfers(conn, window_days=window_days, require_name=require_name)
        names = {a["id"]: a["display_name"]
                 for a in rows(conn, "SELECT id, display_name FROM accounts")}
        out = []
        paired_ids = set()
        for g in groups:
            paired_ids.update((g['out']['id'], g['in']['id']))
            out.append({
                "group": g["group"], "amount": g["amount"], "date": g["date"],
                "from": names.get(g["from"], g["from"]),
                "to": names.get(g["to"], g["to"]),
                "from_desc": g["out"]["description"], "to_desc": g["in"]["description"],
                "matched": True, "txn_id": g['out']['id'],
                "counterparty_imported": True, "evidence": g.get('evidence'),
            })
        for t in rows(conn, "SELECT * FROM transactions WHERE spending_treatment='self_transfer' AND txn_date IS NOT NULL AND COALESCE(currency,'GBP')='GBP' ORDER BY txn_date DESC,id DESC"):
            if t['id'] in paired_ids:
                continue
            own_account = names.get(t['account_id'], t['account_id'])
            other = 'Own account · counterpart not paired'
            classification = merchants._json(t.get('classification'))
            out.append({'group': f"S{t['id']}", 'amount': round(abs(t['amount']), 2),
                        'date': t['txn_date'], 'from': own_account if t['amount'] < 0 else other,
                        'to': other if t['amount'] < 0 else own_account,
                        'from_desc': t['description'] if t['amount'] < 0 else '',
                        'to_desc': t['description'] if t['amount'] > 0 else '',
                        'matched': False, 'txn_id': t['id'], 'counterparty_imported': False,
                        'evidence': classification.get('review_explanation') or classification.get('explanation') or 'Your saved own-transfer classification.'})
        out.sort(key=lambda t: (t['date'] or '', t['group']), reverse=True)
        return out
    finally:
        conn.close()


@app.get("/api/anomalies")
def anomalies(status: Literal["open", "resolved", "learned"] = "open"):
    conn = get_conn()
    try:
        names = {a["id"]: a["display_name"]
                 for a in rows(conn, "SELECT id, display_name FROM accounts")}
        out = []
        candidates = analytics.detect_anomalies(conn) if status == "open" else [
            {**dict(r), **json.loads(r["snapshot"]), "id": r["id"], "merchant": r["merchant"], "category": r["category"]}
            for r in conn.execute("SELECT t.*, ar.snapshot, ar.outcome, ar.reviewed_at FROM anomaly_reviews ar JOIN transactions t ON t.dedupe_key=ar.dedupe_key")
        ]
        if status == "learned":
            candidates = analytics.review_feedback(conn, analytics.detect_anomalies(conn, include_resolved=True))["learned"]
        for a in candidates:
            out.append({
                "id": a["id"], "account": names.get(a["account_id"], a["account_id"]),
                "txn_date": a["txn_date"], "description": a["description"],
                "merchant": a["merchant"], "amount": a["amount"],
                "category": a["category"], "reason": a["reason"], "z": a.get("z"),
                "learning_reason": a.get("learning_reason"), "learning_source_id": a.get("learning_source_id"),
                "explanation": a.get("explanation"), "outcome": a.get("outcome"), "reviewed_at": a.get("reviewed_at"),
                **{field: a.get(field) for field in ('kind', 'baseline_amount', 'threshold_amount',
                   'history_count', 'comparison_basis', 'priority', 'priority_reason',
                   'evidence_strength', 'baseline_from', 'baseline_to', 'comparison_transaction_ids',
                   'method', 'evidence_note')},
            })
        out.sort(key=lambda r: r["txn_date"] or "", reverse=True)
        return out
    finally:
        conn.close()


@app.get("/api/advice")
def advice():
    conn = get_conn()
    try:
        return analytics.savings_advice(conn)
    finally:
        conn.close()


def _investment_scope(conn, account):
    from .investment_accounts import selected_accounts
    # Direct Python calls retain the endpoint's no-filter default.
    account = account if isinstance(account, list) else None
    try:
        return selected_accounts(conn, account)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/investments")
def investments(account: Optional[list[str]] = Query(default=None)):
    conn = get_conn()
    try:
        data = analytics.investments_bundle(conn, _investment_scope(conn, account))
        if data is None:
            raise HTTPException(404, "No investment account found")
        return data
    finally:
        conn.close()


@app.get("/api/valuation")
def valuation(refresh: bool = False, account: Optional[list[str]] = Query(default=None)):
    conn = get_conn()
    try:
        data = analytics.valuation(conn, refresh=refresh, account_ids=_investment_scope(conn, account))
        if data is None:
            raise HTTPException(404, "No investment account found")
        return data
    finally:
        conn.close()


@app.get("/api/investments/trades")
def investment_trades(isin: str = Query(pattern=r"^[A-Z0-9]{12}$"),
                      limit: int = Query(100, ge=1, le=500),
                      offset: int = Query(0, ge=0),
                      account: Optional[list[str]] = Query(default=None)):
    conn = get_conn()
    try:
        return trade_history(conn, isin, limit, offset, _investment_scope(conn, account))
    finally:
        conn.close()


# ----- writes: category override, rules, recompute -----------------------------

class CategoryUpdate(BaseModel):
    category: str = Field(min_length=1, max_length=120)
    merchant: Optional[str] = Field(default=None, min_length=1, max_length=120)
    note: Optional[str] = Field(default=None, max_length=2000)


@app.post("/api/transactions/{txn_id}/category")
def set_category(txn_id: int, body: CategoryUpdate):
    save_transaction_review(txn_id, TransactionReview(category=body.category,
        merchant=body.merchant, note=body.note))
    return {"ok": True, "id": txn_id, "category": body.category}


class Rule(BaseModel):
    pattern: str = Field(min_length=1, max_length=256)
    category: str = Field(min_length=1, max_length=120)
    is_regex: bool = False
    priority: int = 100


@app.get("/api/rules")
def list_rules():
    conn = get_conn()
    try:
        return rows(conn, "SELECT * FROM rules ORDER BY priority, id")
    finally:
        conn.close()


@app.post("/api/rules")
def add_rule(rule: Rule):
    if rule.is_regex:
        try:
            re.compile(rule.pattern)
        except re.error as exc:
            raise HTTPException(422, "Invalid regular expression") from exc
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO rules(pattern,is_regex,category,priority) VALUES(?,?,?,?)",
            (rule.pattern, int(rule.is_regex), rule.category, rule.priority))
        applied = apply_categorization(conn)
        analytics.recompute_all(conn, commit=False)
        conn.commit()
        return {"ok": True, "id": cur.lastrowid, "applied": applied}
    finally:
        conn.close()


@app.delete("/api/rules/{rule_id}")
def delete_rule(rule_id: int):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM rules WHERE id=?", (rule_id,))
        apply_categorization(conn)
        analytics.recompute_all(conn, commit=False)
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def _apply_rule(conn, rule: Rule) -> int:
    """Apply a rule to existing transactions that aren't user-overridden."""
    targets = rows(conn,
        "SELECT id, description, merchant FROM transactions WHERE category_source!='user'")
    ids = []
    if rule.is_regex:
        rx = re.compile(rule.pattern, re.I)
        match = lambda s: bool(rx.search(s))
    else:
        pat = rule.pattern.lower()
        match = lambda s: pat in s.lower()
    for t in targets:
        if match(f"{t['description'] or ''} {t['merchant'] or ''}"):
            ids.append(t["id"])
    if ids:
        conn.executemany(
            "UPDATE transactions SET category=?, category_source='rule' WHERE id=?",
            [(rule.category, i) for i in ids])
    return len(ids)


class RecomputeOpts(BaseModel):
    transfer_window: int = Field(default=3, ge=0, le=30)
    require_name: bool = False


@app.post("/api/recompute")
def recompute(opts: RecomputeOpts = RecomputeOpts()):
    conn = get_conn()
    try:
        apply_categorization(conn)
        result = analytics.recompute_all(
            conn, transfer_window=opts.transfer_window, require_name=opts.require_name, commit=False)
        # Reassert each saved nullable review field after analytics relabels transfers.
        apply_categorization(conn)
        conn.commit()
        return result
    finally:
        conn.close()


@app.get("/api/analytics")
def analytics_view(account: Optional[str] = None, date_from: Optional[date] = None,
                   date_to: Optional[date] = None, exclude_outliers: bool = False,
                   outlier_z: float = Query(3.5, ge=1, le=10)):
    conn = get_conn()
    try:
        if account and not conn.execute('SELECT 1 FROM accounts WHERE id=?', (account,)).fetchone():
            raise HTTPException(404, 'Account not found')
        return analytics.analytics_bundle(conn, account=account,
            date_from=date_from.isoformat() if date_from else None,
            date_to=date_to.isoformat() if date_to else None,
            exclude_outliers=exclude_outliers, outlier_z=outlier_z)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        conn.close()


@app.get("/api/spending-explorer")
def spending_explorer_view(account: Optional[str] = None, date_from: Optional[date] = None,
                           date_to: Optional[date] = None, exclude_outliers: bool = False,
                           outlier_z: float = Query(3.5, ge=1, le=10)):
    from .spending_explorer import explore
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        if account and not conn.execute('SELECT 1 FROM accounts WHERE id=?', (account,)).fetchone():
            raise HTTPException(404, 'Account not found')
        return explore(conn, account, date_from.isoformat() if date_from else None,
                       date_to.isoformat() if date_to else None, exclude_outliers, outlier_z)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        conn.close()


# ----- persistent view preferences --------------------------------------------

_PREFERENCE_KEYS = {'spending', 'reviews', 'transactions', 'planning', 'appearance', 'investment-filters'}


@app.get('/api/preferences/{key}')
def get_preferences(key: str):
    if key not in _PREFERENCE_KEYS:
        raise HTTPException(422, 'Unknown preferences group')
    conn = get_conn()
    try:
        row = conn.execute('SELECT value,updated_at FROM preferences WHERE key=?', (key,)).fetchone()
        return {'key': key, 'value': json.loads(row['value']) if row else {},
                'updated_at': row['updated_at'] if row else None}
    finally:
        conn.close()


@app.put('/api/preferences/{key}')
def save_preferences(key: str, value: dict):
    if key not in _PREFERENCE_KEYS:
        raise HTTPException(422, 'Unknown preferences group')
    try:
        payload = json.dumps(value, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, 'Preferences must contain finite JSON values') from exc
    if len(payload) > 16000:
        raise HTTPException(422, 'Preferences exceed the 16 KB limit')
    conn = get_conn()
    try:
        conn.execute('INSERT INTO preferences(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP', (key, payload))
        conn.commit()
        return {'ok': True, 'key': key, 'value': value}
    finally:
        conn.close()


# ----- merchant identities and persistent transaction review -------------------

@app.get('/api/merchants/inbox')
def merchant_review_inbox():
    conn = get_conn()
    try:
        return merchant_inbox.inbox(conn)
    finally:
        conn.close()


def _merchant_classification(body, save=False):
    conn = get_conn()
    try:
        return (merchant_inbox.save if save else merchant_inbox.preview)(conn, body)
    except merchants.AliasConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        conn.close()


@app.post('/api/merchants/classify/preview')
def preview_merchant_classification(body: merchant_inbox.QuickClassification):
    return _merchant_classification(body)


@app.post('/api/merchants/classify')
def classify_merchant(body: merchant_inbox.QuickClassification):
    return _merchant_classification(body, save=True)


class MerchantAlias(BaseModel):
    pattern: str = Field(min_length=1, max_length=256)
    match_mode: Literal['exact', 'prefix', 'contains'] = 'exact'
    context: Literal['any', 'transfer', 'card', 'funding', 'spending', 'spending_card', 'spending_transfer', 'spending_bank'] = 'any'


class MerchantUpdate(BaseModel):
    canonical_name: str = Field(min_length=1, max_length=120)
    kind: Literal['unknown', 'merchant', 'person', 'self', 'financial'] = 'merchant'
    category: Optional[str] = Field(default=None, min_length=1, max_length=120)
    spending_treatment: Literal['normal', 'self_transfer', 'reimbursement'] = 'normal'
    aliases: list[MerchantAlias] = Field(min_length=1, max_length=merchants.MAX_ALIASES_PER_MERCHANT)
    source_url: Optional[str] = Field(default=None, max_length=1000)
    notes: Optional[str] = Field(default=None, max_length=2000)
    enabled: bool = True
    expected_revision: Optional[str] = Field(default=None, min_length=64, max_length=64)


def _save_merchant(conn, body, merchant_id=None):
    if not body.canonical_name.strip() or any(not merchants.key(a.pattern) for a in body.aliases):
        raise HTTPException(422, 'Merchant names and aliases must contain letters or numbers')
    if any(alias.context == 'spending_bank' and alias.match_mode != 'exact' for alias in body.aliases):
        raise HTTPException(422, 'Full bank descriptions must use exact matching. Confirm each own-transfer description separately.')
    if body.source_url and not re.match(r'^https?://[^\s]+$', body.source_url):
        raise HTTPException(422, 'Source URL must start with http:// or https://')
    if body.kind == 'self' and body.spending_treatment != 'self_transfer':
        raise HTTPException(422, 'An own-account merchant rule must use own-transfer treatment. Review an individual transaction to count an exception in spending.')
    aliases = {(merchants.key(a.pattern), a.match_mode, a.context): a for a in body.aliases}
    if len(aliases) != len(body.aliases):
        raise HTTPException(422, 'Duplicate aliases after normalizing spacing, punctuation and case')
    values = (body.canonical_name.strip(), body.kind, body.category, body.spending_treatment,
              body.source_url, body.notes, int(body.enabled))
    conn.execute('BEGIN IMMEDIATE')
    try:
        current = None
        existing_rows = []
        if merchant_id is not None:
            current = conn.execute('SELECT * FROM merchants WHERE id=?', (merchant_id,)).fetchone()
            if not current:
                raise HTTPException(404, 'Merchant rule not found')
            existing_rows = [dict(row) for row in conn.execute('SELECT * FROM merchant_aliases WHERE merchant_id=? ORDER BY id', (merchant_id,))]
            revision = merchants.merchant_revision({**dict(current), 'aliases': existing_rows})
            if body.expected_revision is not None and body.expected_revision != revision:
                raise HTTPException(409, 'This saved name or its aliases changed in another window. Reopen the latest rule before saving; your draft has not been applied.')
        existing = {}
        for row in existing_rows:
            existing.setdefault((merchants.key(row['pattern']), row['match_mode'], row['context']), row)

        # Reserve the writer before validating ownership. Context names overlap:
        # funding can be a transfer or debit-card payment, while spending scopes
        # are narrower versions of the original card/transfer scopes.
        competitors = merchants._prepared(conn)
        transaction_rows = None
        def source_rows():
            nonlocal transaction_rows
            if transaction_rows is None:
                has_broker = bool(conn.execute("SELECT 1 FROM accounts WHERE account_type='investment' AND lower(replace(bank,' ',''))='trading212'").fetchone())
                transaction_rows = [merchants._source_row(row) for row in conn.execute('SELECT t.*,a.account_type,a.bank FROM transactions t JOIN accounts a ON a.id=t.account_id')]
                for row in transaction_rows:
                    row['_has_broker_account'] = has_broker
            return transaction_rows

        for alias_key, alias in aliases.items():
            if alias.match_mode != 'exact' or (alias_key in existing and current['enabled']) or not body.enabled:
                continue
            proposed = {'pattern': alias.pattern, 'match_mode': 'exact', 'context': alias.context,
                        'canonical_name': body.canonical_name, 'kind': body.kind, 'category': body.category,
                        'spending_treatment': body.spending_treatment, 'source': 'user', 'source_url': body.source_url,
                        'notes': '', 'merchant_id': merchant_id or -1, 'id': -1, 'confidence': 1}
            owners = [other for other in competitors if other['merchant_id'] != merchant_id
                      and other['match_mode'] == 'exact' and merchants.key(other['pattern']) == alias_key[0]]
            for other in owners:
                contexts = {alias.context, other['context']}
                same_scope = alias.context.removeprefix('spending_') == other['context'].removeprefix('spending_')
                overlaps = same_scope or bool(contexts & {'any', 'spending', 'funding'})
                if not overlaps:
                    overlaps = any(merchants.classify(row, [proposed]) and merchants.classify(row, [other]) for row in source_rows())
                if overlaps:
                    raise HTTPException(409, f'The exact label “{alias.pattern.strip()}” already belongs to {other["canonical_name"]} in an overlapping payment context. Choose that saved name or edit its rule first.')
            # A source label and a longer full description can both match the
            # same row exactly; differing pattern text does not remove ownership.
            other_exact = [other for other in competitors if other['merchant_id'] != merchant_id
                           and other['match_mode'] == 'exact' and merchants.key(other['pattern']) != alias_key[0]]
            if other_exact:
                matches = [row for row in source_rows() if merchants.classify(row, [proposed])]
                owner = next((other for other in other_exact if any(merchants.classify(row, [other]) for row in matches)), None)
                if owner:
                    raise HTTPException(409, f'The label “{alias.pattern.strip()}” matches transactions already assigned to {owner["canonical_name"]} by another exact statement label. Choose that saved name or edit its rule first.')

        if current is None:
            merchant_id = conn.execute("INSERT INTO merchants(canonical_name,kind,category,spending_treatment,source_url,notes,enabled,source) VALUES(?,?,?,?,?,?,?,'user')", values).lastrowid
        else:
            fields = ('canonical_name', 'kind', 'category', 'spending_treatment', 'source_url', 'notes', 'enabled')
            definition_changed = tuple(current[field] for field in fields) != values
            source = 'user' if definition_changed else current['source']
            conn.execute('UPDATE merchants SET canonical_name=?,kind=?,category=?,spending_treatment=?,source_url=?,notes=?,enabled=?,source=?,updated_at=CURRENT_TIMESTAMP WHERE id=?', (*values, source, merchant_id))
            if definition_changed:
                # An explicit definition edit makes this the user's rule, as
                # before merging. Alias-only edits retain inherited priorities.
                conn.execute('DELETE FROM merchant_alias_sources WHERE alias_id IN (SELECT id FROM merchant_aliases WHERE merchant_id=?)', (merchant_id,))
        # Remove only missing aliases, before normalized spelling updates, so a
        # legacy duplicate cannot collide with the retained alias's new spelling.
        retained_ids = {existing[key]['id'] for key in aliases if key in existing}
        for row in existing_rows:
            if row['id'] not in retained_ids:
                conn.execute('DELETE FROM merchant_aliases WHERE id=?', (row['id'],))
        for alias_key, alias in aliases.items():
            retained = existing.get(alias_key)
            if retained:
                alias_id = retained['id']
                conn.execute('UPDATE merchant_aliases SET pattern=? WHERE id=?', (alias.pattern.strip(), alias_id))
            else:
                alias_id = conn.execute('INSERT INTO merchant_aliases(merchant_id,pattern,match_mode,context) VALUES(?,?,?,?)',
                    (merchant_id, alias.pattern.strip(), alias.match_mode, alias.context)).lastrowid
                if alias.match_mode == 'exact':
                    conn.execute('INSERT INTO merchant_alias_approvals(alias_id) VALUES(?)', (alias_id,))
                    if body.enabled:
                        merchants.set_suggestion_decision(conn, merchant_id, alias.pattern, 'approved')
        # Explicit removals clear approvals, while dismissals remain independent.
        retained_keys = {key[0] for key in aliases if key[1] == 'exact'}
        for decision in conn.execute("SELECT label_key FROM merchant_suggestion_decisions WHERE merchant_id=? AND status='approved'", (merchant_id,)).fetchall():
            if decision['label_key'] not in retained_keys:
                conn.execute("DELETE FROM merchant_suggestion_decisions WHERE merchant_id=? AND label_key=? AND status='approved'", (merchant_id, decision['label_key']))
        applied = apply_categorization(conn)
        analytics.recompute_all(conn, commit=False)
        item = next(item for item in merchants.list_merchants(conn, include_disabled=True) if item['id'] == merchant_id)
        conn.commit()
        return {'ok': True, 'id': merchant_id, 'applied': applied, 'merchant': item}
    except Exception:
        conn.rollback()
        raise


@app.get('/api/merchants')
def merchant_rules():
    conn = get_conn()
    try:
        return {'items': merchants.list_merchants(conn), 'suggestions': merchants.suggestions(conn),
                'dismissed_suggestions': merchants.dismissed_suggestions(conn),
                'privacy_note': 'Classification runs locally. Similar spelling is a suggestion until you approve an alias. Public research sources describe the business, not the exact item purchased.'}
    finally:
        conn.close()


@app.get('/api/merchants/{merchant_id}/activity')
def merchant_activity(merchant_id: int):
    conn = get_conn()
    try:
        return banking_insights.merchant_activity(conn, merchant_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    finally:
        conn.close()


@app.post('/api/merchants/similar')
def similar_merchant_names(body: merchant_inbox.SimilarNamesQuery):
    conn = get_conn()
    try:
        return merchant_inbox.similar_names(conn, body)
    except merchants.AliasConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        conn.close()


@app.post('/api/merchants')
def add_merchant(body: MerchantUpdate):
    conn = get_conn()
    try:
        return _save_merchant(conn, body)
    finally:
        conn.close()


class AliasApproval(BaseModel):
    pattern: str = Field(min_length=1, max_length=256)
    context: Optional[Literal['any', 'transfer', 'card', 'funding']] = None
    example_id: Optional[int] = Field(default=None, ge=1)


class SuggestionDecision(BaseModel):
    pattern: str = Field(min_length=1, max_length=256)


@app.post('/api/merchants/{merchant_id}/aliases')
def approve_merchant_alias(merchant_id: int, body: AliasApproval):
    conn = get_conn()
    try:
        return merchants.approve_alias(conn, merchant_id, body.pattern, body.context, body.example_id)
    except merchants.AliasConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        conn.close()


@app.post('/api/merchants/{merchant_id}/suggestions/dismiss')
def dismiss_merchant_suggestion(merchant_id: int, body: SuggestionDecision):
    conn = get_conn()
    try:
        merchants.set_suggestion_decision(conn, merchant_id, body.pattern, 'dismissed')
        conn.commit()
        return {'ok': True, 'merchant_id': merchant_id, 'pattern': body.pattern}
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        conn.close()


@app.post('/api/merchants/{merchant_id}/suggestions/restore')
def restore_merchant_suggestion(merchant_id: int, body: SuggestionDecision):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM merchant_suggestion_decisions WHERE label_key=? AND merchant_id=? AND status='dismissed'", (merchants.key(body.pattern), merchant_id))
        conn.commit()
        return {'ok': True}
    finally:
        conn.close()


@app.put('/api/merchants/{merchant_id}')
def update_merchant(merchant_id: int, body: MerchantUpdate):
    conn = get_conn()
    try:
        return _save_merchant(conn, body, merchant_id)
    finally:
        conn.close()


@app.delete('/api/merchants/{merchant_id}')
def delete_merchant(merchant_id: int):
    conn = get_conn()
    try:
        cur = conn.execute('UPDATE merchants SET enabled=0,updated_at=CURRENT_TIMESTAMP WHERE id=?', (merchant_id,))
        if not cur.rowcount:
            raise HTTPException(404, 'Merchant rule not found')
        apply_categorization(conn)
        analytics.recompute_all(conn, commit=False)
        conn.commit()
        return {'ok': True}
    finally:
        conn.close()


class TransactionReview(BaseModel):
    resolution: Optional[Literal["expected", "issue", "pending"]] = None
    category: Optional[str] = Field(default=None, min_length=1, max_length=120)
    merchant: Optional[str] = Field(default=None, min_length=1, max_length=120)
    merchant_kind: Optional[Literal['unknown', 'merchant', 'person', 'self', 'financial']] = None
    spending_treatment: Optional[Literal['normal', 'self_transfer', 'reimbursement']] = None
    exclude_from_analysis: Optional[bool] = None
    note: Optional[str] = Field(default=None, max_length=2000)


def _review_result(conn, txn_id):
    row = conn.execute('SELECT t.*,a.display_name AS account,a.account_type FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE t.id=?', (txn_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'Transaction not found')
    review = conn.execute('SELECT * FROM overrides WHERE dedupe_key=?', (row['dedupe_key'],)).fetchone()
    transaction = merchants.public_transaction(row)
    # The raw statement remains on disk/in the DB; no need to expose full parser
    # blobs or stable internal keys in a transaction review response.
    for field in ('raw', 'meta', 'dedupe_key'):
        transaction.pop(field, None)
    saved = dict(review) if review else {}
    saved.pop('dedupe_key', None)
    decision = conn.execute('SELECT outcome FROM anomaly_reviews WHERE dedupe_key=?', (row['dedupe_key'],)).fetchone()
    if decision:
        saved['resolution'] = decision['outcome']
    return {'transaction': transaction, 'review': saved,
            'bank_evidence': banking_insights.transaction_evidence(conn, txn_id)}


@app.get('/api/transactions/{txn_id}/review')
def transaction_review(txn_id: int):
    conn = get_conn()
    try:
        return _review_result(conn, txn_id)
    finally:
        conn.close()


@app.post('/api/transactions/{txn_id}/review')
def save_transaction_review(txn_id: int, body: TransactionReview):
    conn = get_conn()
    try:
        # Serialize read/merge/write so concurrent partial reviews retain each
        # other's fields and never upgrade a stale WAL snapshot into a writer.
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT dedupe_key FROM transactions WHERE id=?', (txn_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Transaction not found')
        # A few legacy/manual rows have no import key. Assign a stable local key
        # once so multiple reviewed rows cannot accidentally share one override.
        dedupe = row['dedupe_key'] or f'local-review:{txn_id}'
        if not row['dedupe_key']:
            conn.execute('UPDATE transactions SET dedupe_key=? WHERE id=?', (dedupe, txn_id))
        if body.resolution == 'pending':
            conn.execute('DELETE FROM anomaly_reviews WHERE dedupe_key=?', (dedupe,))
            conn.execute('INSERT OR IGNORE INTO anomaly_review_exceptions(dedupe_key) VALUES(?)', (dedupe,))
        elif body.resolution:
            conn.execute('DELETE FROM anomaly_review_exceptions WHERE dedupe_key=?', (dedupe,))
            previous_flag = conn.execute('SELECT snapshot FROM anomaly_reviews WHERE dedupe_key=?', (dedupe,)).fetchone()
            flag = next((a for a in analytics.detect_anomalies(conn, include_resolved=True) if a['id'] == txn_id), None)
            if not flag and not previous_flag:
                raise HTTPException(409, 'This payment is no longer flagged. Refresh the review list.')
            # Store only the explanation/score shown at decision time, not source blobs.
            snapshot = previous_flag['snapshot'] if previous_flag else json.dumps({k: flag.get(k) for k in ('reason', 'z', 'kind', 'explanation', 'baseline_amount', 'threshold_amount', 'history_count', 'comparison_basis')})
            conn.execute("INSERT INTO anomaly_reviews(dedupe_key,outcome,snapshot) VALUES(?,?,?) ON CONFLICT(dedupe_key) DO UPDATE SET outcome=excluded.outcome, reviewed_at=CURRENT_TIMESTAMP", (dedupe, body.resolution, snapshot))
        if body.resolution and body.model_fields_set == {'resolution'}:
            # A queue decision does not change classification or cash flow.
            # Refresh queue markers (including similar inferred matches), not spending analytics.
            flags = analytics.detect_anomalies(conn)
            conn.execute('UPDATE transactions SET is_anomaly=0 WHERE is_anomaly=1')
            conn.executemany('UPDATE transactions SET is_anomaly=1 WHERE id=?', [(a['id'],) for a in flags])
            result = _review_result(conn, txn_id)
            conn.commit()
            return result
        # Preserve the parser merchant before applying the first review.
        apply_categorization(conn)
        previous = conn.execute('SELECT * FROM overrides WHERE dedupe_key=?', (dedupe,)).fetchone()
        fields = ('category', 'merchant', 'merchant_kind', 'spending_treatment', 'exclude_from_analysis', 'note')
        saved = dict(previous) if previous else {k: None for k in fields}
        saved.update(body.model_dump(exclude_unset=True))
        conn.execute('INSERT INTO overrides(dedupe_key,category,merchant,merchant_kind,spending_treatment,exclude_from_analysis,note) VALUES(?,?,?,?,?,?,?) ON CONFLICT(dedupe_key) DO UPDATE SET category=excluded.category,merchant=excluded.merchant,merchant_kind=excluded.merchant_kind,spending_treatment=excluded.spending_treatment,exclude_from_analysis=excluded.exclude_from_analysis,note=excluded.note',
                     (dedupe, *(saved[k] for k in fields)))
        apply_categorization(conn)
        analytics.recompute_all(conn, commit=False)
        result = _review_result(conn, txn_id)
        conn.commit()
        return result
    finally:
        conn.close()


@app.delete('/api/transactions/{txn_id}/review')
def reset_transaction_review(txn_id: int):
    conn = get_conn()
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT dedupe_key FROM transactions WHERE id=?', (txn_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Transaction not found')
        conn.execute('DELETE FROM overrides WHERE dedupe_key=?', (row['dedupe_key'],))
        conn.execute("UPDATE transactions SET category_source='auto' WHERE id=?", (txn_id,))
        apply_categorization(conn)
        analytics.recompute_all(conn, commit=False)
        result = _review_result(conn, txn_id)
        conn.commit()
        return result
    finally:
        conn.close()


# Stale clients receive an explicit retirement response without parsing files or
# touching the ledger. Saved documents and operator restore tools are unchanged.
@app.post("/api/upload", include_in_schema=False)
def retired_upload():
    raise HTTPException(410, "Statement uploads have been retired. Use Banks & accounts or Investments to refresh connected accounts.")


@app.get('/api/planning')
def planning_view(years: int = Query(10, ge=1, le=50),
                  annual_return: float = Query(5, ge=-50, le=50),
                  inflation: float = Query(2, ge=0, le=20),
                  monthly_contribution: Optional[float] = Query(None, ge=-1000000, le=1000000),
                  initial_wealth: Optional[float] = Query(None, ge=-1e9, le=1e9),
                  target: float = Query(100000, gt=0, le=1e10),
                  baseline_method: Literal['mean', 'median'] = 'mean',
                  exclude_outliers: bool = False,
                  one_off_amount: float = Query(0, ge=0, le=1e9),
                  one_off_month: int = Query(1, ge=1, le=600)):
    conn = get_conn()
    try:
        return planning.planning_bundle(conn, years=years, annual_return=annual_return,
            inflation=inflation, monthly_contribution=monthly_contribution,
            initial_wealth=initial_wealth, target=target, baseline_method=baseline_method,
            exclude_outliers=exclude_outliers, one_off_amount=one_off_amount, one_off_month=one_off_month)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        conn.close()


class SavedScenarioInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    parameters: dict
    id: Optional[int] = Field(default=None, ge=1)


@app.get('/api/scenarios')
def scenarios_list():
    conn = get_conn()
    try:
        return planning.saved_scenarios(conn)
    finally:
        conn.close()


@app.post('/api/scenarios')
def scenarios_save(payload: SavedScenarioInput):
    conn = get_conn()
    try:
        if payload.id and not conn.execute('SELECT 1 FROM planning_scenarios WHERE id=?', (payload.id,)).fetchone():
            raise HTTPException(404, 'Saved scenario not found')
        return planning.save_scenario(conn, payload.name, payload.parameters, payload.id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        conn.close()


@app.delete('/api/scenarios/{scenario_id}')
def scenarios_delete(scenario_id: int):
    conn = get_conn()
    try:
        if not planning.delete_scenario(conn, scenario_id):
            raise HTTPException(404, 'Saved scenario not found')
        return {'ok': True}
    finally:
        conn.close()


@app.get("/api/health")
def health():
    payload, status = database_readiness(DB_PATH)
    return JSONResponse(payload, status_code=status, headers={'Cache-Control': 'no-store'})


@app.get('/api/revision')
def revision():
    payload, status = database_revision(DB_PATH)
    return JSONResponse(payload, status_code=status, headers={'Cache-Control': 'no-store'})


# ----- serve built frontend (if present) --------------------------------------

_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if _DIST.exists():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="frontend")
else:
    @app.get("/")
    def root():
        return JSONResponse({"service": "finance-app", "docs": "/docs",
                             "note": "frontend not built yet (frontend/dist missing)"})

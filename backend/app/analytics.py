"""Local intelligence: recurring/subscription detection, cross-bank transfer
matching, anomaly detection, and savings advice. All deterministic & offline.
"""
from __future__ import annotations
import json
import calendar
import math
import re
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher

from .recurring_identity import grouped_payments
from .recurring_model import amount_profile, fit_schedule
from .workspace import category_maps, annotate_category, semantic

PRICE_TTL = timedelta(minutes=15)

# ----- helpers -----------------------------------------------------------------

def _d(s: str | None) -> date | None:
    try:
        return datetime.fromisoformat(s).date() if s else None
    except (TypeError, ValueError):
        return None


def _rows(conn):
    cursor = conn.execute(
        "SELECT t.*, a.bank, a.account_type FROM transactions t"
        " JOIN accounts a ON a.id=t.account_id"
        " WHERE txn_date IS NOT NULL ORDER BY txn_date, t.id")
    columns = [column[0] for column in cursor.description]
    date_index = columns.index("txn_date")
    # sqlite3.Row name lookups scan columns; positional conversion avoids that
    # repeated work while retaining the identical fields and source values.
    records = [record for row in cursor if _d(row[date_index])
               and (record := dict(zip(columns, row))).get('currency') in (None, 'GBP')]
    _, catalog = category_maps(conn)
    from .account_settings import effective_settings
    settings = {ident: effective_settings(conn, ident) for ident in {r['account_id'] for r in records}}
    for record in records:
        annotate_category(record, catalog)
        account = settings[record['account_id']]
        record['account_excluded'] = account['archived'] or not account['include_in_reports']
        record['account_ownership_share'] = account['ownership_share']
    return records


def _spending_rows(conn):
    return [r for r in cashflow_rows(conn) if r["amount"] < 0]


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a or "", b or "").ratio()


# ----- recurring / subscriptions ----------------------------------------------

# Cadence centres (days) and the default ± day tolerance for matching each.
CADENCES = [("weekly", 7.0), ("fortnightly", 14.0), ("monthly", 30.4),
            ("bi-monthly", 60.8), ("quarterly", 91.3), ("annual", 365.0)]
DEFAULT_DAY_TOL = {"weekly": 2, "fortnightly": 2, "monthly": 5,
                   "bi-monthly": 3, "quarterly": 5, "annual": 15}


def detect_recurring(conn, min_occurrences=3, amount_tol=0.10, day_tol=None,
                     regularity=0.5, as_of: date | None = None, *, source_rows=None, spending_rows=None):
    """Detect recurring account/merchant charges from observed statement history.

    A missing expected payment is evidence of an ended series only when that
    account has newer observations. Old exports are labelled data_stale, rather
    than being treated as proof that a subscription was cancelled.
    """
    tol = {**DEFAULT_DAY_TOL, **(day_tol or {})}
    today = as_of or date.today()
    source_rows = source_rows if source_rows is not None else _rows(conn)
    if spending_rows is None:
        spending_rows = [component for row in source_rows
                         if (component := _household_component(row)) is not None and component['amount'] < 0]
    observed = {}
    account_types = {row["id"]:row["account_type"] for row in conn.execute("SELECT id,account_type FROM accounts")}
    for row in source_rows:
        if row["account_type"] != "investment" and _d(row["txn_date"]) > today:
            continue
        observed[row["account_id"]] = max(observed.get(row["account_id"], date.min), _d(row["txn_date"]))
    for row in conn.execute("SELECT s.account_id, MAX(s.period_end) AS last FROM statements s "
                            "JOIN accounts a ON a.id=s.account_id "
                            "WHERE a.account_type='investment' OR s.period_end<=? GROUP BY s.account_id",
                            (today.isoformat(),)):
        if _d(row["last"]):
            observed[row["account_id"]] = max(observed.get(row["account_id"], date.min), _d(row["last"]))
    eligible_rows = []
    for row in spending_rows:
        if row["account_type"] != "investment" and _d(row["txn_date"]) > today:
            continue
        if row["merchant"]:
            eligible_rows.append(row)

    results = []
    for account_id, merchant, identity, txns in grouped_payments(eligible_rows):
        by_date = defaultdict(float)
        for t in txns:
            by_date[_d(t["txn_date"])] += abs(t["amount"])
        dates = sorted(by_date)
        if len(dates) < max(2, min_occurrences):
            continue
        gaps = [(right - left).days for left, right in zip(dates, dates[1:])]
        med = statistics.median(gaps)
        category = _mode([t["category"] for t in txns])
        category_kind = _mode([semantic(t) for t in txns])
        bank = account_types.get(account_id) != 'investment'
        bill_like = (category_kind in {'Housing','Subscriptions','Phone & internet','Bills & utilities','Health & education','Software & online services'}
                     or bool(re.search(r'\b(subs?|subscription|membership|premium|prime)\b',merchant,re.I)))
        schedule = fit_schedule(dates, day_tolerance=tol, regularity=regularity,
                                min_occurrences=min_occurrences) if bank and bill_like else None
        cadence = schedule['cadence'] if schedule else _cadence_label(med, tol)
        if cadence is None:
            continue
        if schedule:
            med = schedule['cadence_days']
        # Posting variation is a display window, not proof that an account's
        # later records are complete. Keep the existing conservative grace for
        # stale/ended status instead of equating a narrow window with cancellation.
        win = max(tol.get(cadence, 4), med * 0.2)
        regular = schedule['regularity'] if schedule else sum(abs(g - med) <= win for g in gaps) / len(gaps)
        if regular < regularity:
            continue
        amounts = [by_date[d] for d in dates]
        # Compare the last two observations with earlier history, so a sustained
        # price increase is not absorbed into its own comparison baseline.
        typical = statistics.median(amounts[:-2] or amounts)
        current = statistics.median(amounts[-2:])
        matched_amounts = [by_date[d] for d in schedule['schedule_dates']] if schedule else amounts
        profile = amount_profile(matched_amounts, change_threshold=amount_tol) if bank and bill_like else None
        if profile and len(matched_amounts) < len(amounts):
            profile['amount_basis'] = (f"Uses {len(matched_amounts)} payment dates matching this schedule. "
                                       + profile['amount_basis'])
        if profile:
            typical, current = profile['typical_amount'], profile['current_amount']
        last = dates[-1]
        try:
            next_expected = schedule['next_expected'] if schedule else last + timedelta(days=round(med))
        except OverflowError:
            continue  # No representable next date at the supported date boundary.
        end = observed.get(account_id, last)
        ended = (end - next_expected).days > win
        changed = profile['price_change_confirmed'] if profile else bool(typical and abs(current - typical) / typical > amount_tol)
        typical_band = max(current*.2,1)
        recent_amounts = amounts[-6:]
        consistency = profile['amount_consistency'] if profile else sum(abs(amount-current) <= typical_band for amount in recent_amounts)/len(recent_amounts)
        # With only three matched dates, two vaguely similar amounts are not
        # enough to promote usage billing to a fixed commitment. A supported
        # recent price regime already has its own consistent amount evidence.
        required_consistency = 1 if profile and len(matched_amounts) <= 3 else 2/3
        pattern_type = ('variable_cost' if category_kind == 'Fees & interest' or (bill_like and consistency < required_consistency)
                        else 'regular_payment' if bill_like else 'spending_habit')
        forecast = pattern_type == 'regular_payment' and regular >= .7 and not ended and (today-end).days <= med+win and (not bank or schedule is not None)
        forecast_reason = ('Repeated bill-like payments with consistent recent amounts and timing.' if forecast
                           else 'Spending habits do not establish a scheduled bill.' if pattern_type == 'spending_habit'
                           else 'Variable charges or transaction fees do not establish a fixed next payment.' if pattern_type == 'variable_cost'
                           else 'More recent observations are needed.' if (today-end).days > med+win
                           else 'No recent payment was observed, or timing is not consistent enough for a forecast.')
        evidence = ({'pattern_type':pattern_type,'forecast_eligible':forecast,'forecast_reason':forecast_reason,
                     'amount_consistency':round(consistency,3),'regularity':round(regular,3),'evidence_count':len(dates),
                     'observed_interval':f'{dates[0].isoformat()} to {last.isoformat()}'}
                    if bank else {})
        if bank:
            evidence.update(series_identity=json.dumps(identity), merchant_labels=sorted({t['merchant'] for t in txns}))
        if profile:
            evidence.update(profile)
        if schedule:
            evidence.update({key: value.isoformat() if isinstance(value, date) else value
                             for key, value in schedule.items()
                             if key not in {'next_expected', 'cadence', 'cadence_days', 'regularity', 'normalized_monthly_factor', 'schedule_dates'}})
            evidence['schedule_basis'] = ('Calendar month-end pattern.' if schedule['anchor_kind'] == 'month_end'
                                          else f"Calendar pattern around day {schedule['anchor_day']}." if schedule['anchor_day']
                                          else f"Observed {cadence} payment pattern.")
            if ended:
                evidence.update(next_expected_earliest=None, next_expected_latest=None)
        monthly_factor = schedule['normalized_monthly_factor'] if schedule else 365.25 / 12 / med
        results.append({
            **evidence,
            "account_id": account_id, "merchant": merchant,
            "cadence_days": round(med, 1), "cadence": cadence,
            "typical_amount": round(typical, 2), "current_amount": round(current, 2),
            "amount_spread": round((max(amounts) - min(amounts)) / typical, 2) if typical else 0,
            "n_occurrences": len(dates), "first_seen": dates[0].isoformat(),
            "last_seen": last.isoformat(), "observed_through": end.isoformat(),
            "next_expected": next_expected.isoformat() if not ended else None,
            "data_stale": (today - end).days > med + win,
            "category": category, "category_semantic": category_kind,
            "monthly_cost": 0.0 if ended else round(current * monthly_factor, 2),
            "historical_monthly_cost": round(current * monthly_factor, 2),
            "status": "ended" if ended else "price_change" if changed else "active",
            "txn_ids": [t["id"] for t in txns],
        })
    return sorted(results, key=lambda r: r["monthly_cost"], reverse=True)


def _cadence_label(days: float, tol: dict | None = None):
    """Match a median gap (days) to the nearest named cadence within its ± tolerance."""
    tol = tol or DEFAULT_DAY_TOL
    best = None
    for name, center in CADENCES:
        d = abs(days - center)
        if d <= tol.get(name, DEFAULT_DAY_TOL[name]) and (best is None or d < best[1]):
            best = (name, d)
    return best[0] if best else None


def _mode(xs):
    xs = [x for x in xs if x]
    return max(sorted(set(xs)), key=xs.count) if xs else None


# ----- cross-bank transfer matching -------------------------------------------

def detect_transfers(conn, window_days=3, require_name=False, name_keywords=None):
    """Conservatively pair corroborated internal transfers, one-to-one.

    Equal amount/date alone is never evidence: that can hide genuine purchases
    and salary. Ambiguous equally plausible pairs are left for review. Categories
    are not changed, and explicit user spending classifications are respected.
    """
    keywords = [k.lower().strip() for k in (name_keywords or []) if k.strip()]

    def reviewed_normal(row):
        if row.get("spending_treatment") != "normal":
            return False
        try:
            classification = json.loads(row.get("classification") or "{}")
        except (TypeError, ValueError):
            return False
        return "spending_treatment" in classification.get("reviewed_fields", [])

    rows = [r for r in _rows(conn) if semantic(r) != "Investments"
            and r.get("merchant_kind") != "person"
            and not _exchange_details(r)[0]
            and not reviewed_normal(r)
            and not (r["category_source"] == "user" and semantic(r) != "Transfers & payments")]
    outs = [r for r in rows if r["amount"] < 0 and r["account_type"] != "credit"]
    ins = [r for r in rows if r["amount"] > 0]
    ins_by_amt = defaultdict(list)
    for r in ins:
        ins_by_amt[round(r["amount"], 2)].append(r)

    def references_bank(row, other):
        text = (row["description"] or "").lower()
        bank = (other["bank"] or "").lower()
        names = {bank} if len(bank) >= 3 else set()
        if bank in {"american express", "amex"}:
            names.update({"american express", "amex"})
        if bank.replace(" ", "") == "trading212":
            names.update({"trading212", "trading 212"})
        return any(name in text for name in names)

    def evidence(row, other):
        text = (row["description"] or "").lower()
        explicit = row.get("spending_treatment") == "self_transfer"
        explicit |= bool(re.search(r"self transfer|between (?:my|your|own) accounts", text))
        explicit |= row["account_type"] == "credit" and bool(re.search(
            r"payment.{0,8}thank you|payment received|direct debit payment|payment by direct debit", text))
        explicit |= row["account_type"] == "investment" and row["type_code"] in {"Deposit", "Withdrawal"}
        return explicit or references_bank(row, other) or any(k in text for k in keywords)

    candidates = []
    for out in outs:
        for incoming in ins_by_amt.get(round(-out["amount"], 2), []):
            if out["account_id"] == incoming["account_id"]:
                continue
            gap = abs((_d(incoming["txn_date"]) - _d(out["txn_date"])).days)
            if gap > window_days:
                continue
            oe, ie = evidence(out, incoming), evidence(incoming, out)
            # A named bank on either side plus an explicit payment/deposit on the
            # other corroborates the direction. Generic bank-payment type codes
            # may supply the other leg, but are never sufficient by themselves.
            transfer_codes = {"BP", "FPO", "FPI", "TFR", "TRF", "SO", "DD"}
            out_leg = oe or out["type_code"] in transfer_codes
            in_leg = ie or incoming["type_code"] in transfer_codes
            if not (out_leg and in_leg and (oe or ie)):
                continue
            if require_name and not (oe and ie):
                continue
            score = (int(oe) + int(ie), -gap)
            candidates.append((score, out, incoming))

    by_out, by_in = defaultdict(list), defaultdict(list)
    for candidate in candidates:
        by_out[candidate[1]["id"]].append(candidate)
        by_in[candidate[2]["id"]].append(candidate)

    def unique_best(candidate, choices):
        best = max(c[0] for c in choices)
        return candidate[0] == best and sum(c[0] == best for c in choices) == 1

    groups = []
    for candidate in sorted(candidates, key=lambda c: (c[1]["txn_date"], c[1]["id"])):
        score, out, incoming = candidate
        if not (unique_best(candidate, by_out[out["id"]]) and unique_best(candidate, by_in[incoming["id"]])):
            continue
        groups.append({"group": f"T{len(groups) + 1}", "amount": round(-out["amount"], 2),
                       "out": out, "in": incoming, "from": out["account_id"],
                       "to": incoming["account_id"], "date": out["txn_date"],
                       "evidence": "Corroborated account transfer; equal amount within date window"})
    return groups


# ----- anomaly detection -------------------------------------------------------

def _legacy_anomalies(conn, z=3.5, min_history=5, include_resolved=False, *, spending_rows=None, target_ids=None):
    """Review flags, not proof of fraud: robust category outliers and duplicates.

    Median absolute deviation avoids a single large charge inflating its own
    baseline. A 10%/£1 scale floor handles identical historical amounts.
    """
    if not math.isfinite(z) or z < 1:
        raise ValueError("Outlier sensitivity must be at least 1")
    rows = spending_rows if spending_rows is not None else _spending_rows(conn)
    anomalies = []
    by_cat = defaultdict(list)
    by_merchant = defaultdict(list)

    def series_key(row):
        name = (row['merchant'] or '').casefold().strip()
        # Bank parsers may retain a directional prefix in the merchant label.
        # Removing it in transfer context joins the same payee's observed history
        # without applying fuzzy identity matching to unrelated businesses.
        from .merchants import _transfer_context
        if _transfer_context(row):
            name = re.sub(r'^(?:(?:transfer|payment) (?:to|from)|to|from)\s+', '', name)
        return name

    series = {}
    for r in rows:
        if r.get("merchant_kind") != "person":
            by_cat[r["category"] or "Uncategorized"].append(r)
        if r["merchant"]:
            series[r["id"]] = series_key(r)
            by_merchant[series[r["id"]]].append(r)
    # A peer group has one median and MAD, irrespective of which member is
    # being assessed. Keep this memo local to this call so every edit/sync is
    # visible immediately and a different sensitivity uses new thresholds.
    peer_statistics = {}
    for t in rows:
        if target_ids is not None and t["id"] not in target_ids:
            continue
        cat = t["category"] or "Uncategorized"
        name = series.get(t["id"], "")
        peer_rows = by_merchant.get(name, [])
        merchant_basis = len(peer_rows) >= max(3, min_history)
        if not merchant_basis:
            # A friend's transfers are not comparable with purchases from shops.
            # Keep them in cash flow; flag only against their own sufficient history.
            if t.get("merchant_kind") == "person" or cat in {'Uncategorized', 'Cash', 'Friends & shared costs'}:
                continue
            similar_days = sorted({_d(r['txn_date']) for r in peer_rows
                                   if abs(abs(r['amount'])-abs(t['amount'])) <= max(abs(t['amount'])*.2, 1)})
            if len(similar_days) >= 3 and (similar_days[-1]-similar_days[0]).days >= 45:
                # Three comparable charges spanning at least 45 days provide
                # evidence of a repeat cost. Keep the five-observation minimum
                # for merchant statistics, but do not compare this established
                # price level with other merchants' much cheaper charges.
                continue
            peer_rows = by_cat[cat]
        if len(peer_rows) < max(3, min_history):
            continue
        group = ("merchant",name) if merchant_basis else ("category",cat)
        if group not in peer_statistics:
            amts = [abs(r["amount"]) for r in peer_rows]
            median = statistics.median(amts)
            mad = statistics.median(abs(a - median) for a in amts)
            scale = max(1.4826 * mad, median * 0.1, 1.0)
            # The same absolute/relative floors apply to every group member.
            threshold = max(median + z * scale, median + 25, median * (1.5 if merchant_basis else 3))
            peer_statistics[group] = median,scale,threshold,len(amts)
        median,scale,threshold,history_count = peer_statistics[group]
        score = (abs(t["amount"]) - median) / scale
        if abs(t["amount"]) >= threshold:
            basis = t["merchant"] if merchant_basis else cat
            anomalies.append({**t, "reason": f"Unusually large for {basis}",
                              "z": round(score, 1), "score_method": "median/MAD",
                              "kind": "large_amount", "baseline_amount": round(median, 2),
                              "threshold_amount": round(threshold, 2), "history_count": history_count,
                              "comparison_basis": "merchant" if merchant_basis else "category",
                              "explanation": f"£{abs(t['amount']):,.2f} versus a median £{median:,.2f} across {history_count} charges to this {'merchant' if merchant_basis else 'category'}. Review threshold £{threshold:,.2f}; a flag is not proof of an error."})
    seen = defaultdict(list)
    for r in rows:
        if target_ids is not None and r["id"] not in target_ids:
            continue
        if not r["merchant"]:
            continue
        key = (r["account_id"], r["merchant"], round(abs(r["amount"]), 2))
        d = _d(r["txn_date"])
        seen[key] = [prev for prev in seen[key] if (d - _d(prev["txn_date"])).days <= 2]
        if seen[key]:
            anomalies.append({**r, "reason": "Possible duplicate charge", "z": None,
                              "kind": "possible_duplicate", "comparison_basis": "same_account_merchant_amount",
                              "explanation": "The same account paid the same merchant the same amount within two days. Repeated legitimate purchases are possible; nothing is deleted."})
        seen[key].append(r)
    out, ids = [], set()
    for a in anomalies:
        if a["id"] not in ids:
            ids.add(a["id"])
            out.append(a)
    return out if include_resolved else review_feedback(conn, out)["open"]


def detect_anomalies(conn, z=3.5, min_history=5, include_resolved=False, *, spending_rows=None):
    """Causal bank review evidence alongside unchanged investment detection."""
    if not math.isfinite(z) or z < 1:
        raise ValueError("Outlier sensitivity must be at least 1")
    from .anomaly_learning import detect
    source = spending_rows if spending_rows is not None else _spending_rows(conn)
    bank = detect(source,z=z,min_history=min_history)
    # The historic investment comparator included the whole household dataset.
    # Preserve its inputs and outputs exactly while evolving banking separately.
    investment_ids = {row["id"] for row in source if row["account_type"] == "investment"}
    investment = (_legacy_anomalies(conn,z,min_history,True,spending_rows=source,target_ids=investment_ids)
                  if investment_ids else [])
    out = bank + investment
    return out if include_resolved else review_feedback(conn,out)['open']


def refresh_bank_anomaly_flags(conn, *, commit=False):
    """Align only bank review markers; preserve every other stored field.

    Caller-owned transactions are the default so deployment can audit and roll
    back changes. Investment markers and recurring/transfer links stay intact.
    """
    wanted = {row['id'] for row in detect_anomalies(conn) if row['account_type'] != 'investment'}
    updates = [(int(row['id'] in wanted),row['id']) for row in conn.execute(
        "SELECT t.id,t.is_anomaly FROM transactions t JOIN accounts a ON a.id=t.account_id "
        "WHERE a.account_type<>'investment'") if row['is_anomaly'] != int(row['id'] in wanted)]
    conn.executemany('UPDATE transactions SET is_anomaly=? WHERE id=?',updates)
    if commit:
        conn.commit()
    return {'changed':len(updates),'open_bank_flags':len(wanted)}


def review_feedback(conn, candidates):
    """Case-based feedback, not a calibrated probability or trained classifier.

    Only explicit human outcomes teach the model; inferred outcomes never teach
    other outcomes. Reopening protects a row from automatic suppression.
    """
    from .merchants import _transfer_context
    decisions = {r['dedupe_key']: dict(r) for r in conn.execute('SELECT * FROM anomaly_reviews')}
    exceptions = {r[0] for r in conn.execute('SELECT dedupe_key FROM anomaly_review_exceptions')}
    examples = []
    # Explicit outcomes are sparse. Read those rows directly rather than
    # rebuilding every household payment just to find reviewed examples.
    for source in conn.execute("SELECT t.*,a.bank,a.account_type FROM transactions t JOIN accounts a ON a.id=t.account_id JOIN anomaly_reviews ar ON ar.dedupe_key=t.dedupe_key"):
        row = _household_component(dict(source))
        if row is None or row['amount'] >= 0:
            continue
        decision = decisions[row['dedupe_key']]
        try:
            snapshot = json.loads(decision['snapshot'])
        except (TypeError,ValueError):
            continue  # Explicit outcome still wins; unusable evidence never teaches.
        if isinstance(snapshot,dict):
            examples.append((row,decision,snapshot))

    def comparable(a, b, snapshot):
        if not a.get('merchant') or not b.get('merchant'):
            return False
        if a.get('merchant_kind') not in ('merchant', 'financial'):
            return False
        if (a['account_id'], a.get('category'), a.get('merchant_kind'), _transfer_context(a)) != (b['account_id'], b.get('category'), b.get('merchant_kind'), _transfer_context(b)):
            return False
        if a['merchant'].casefold().strip() != b['merchant'].casefold().strip():
            return False
        if a.get('kind') != snapshot.get('kind'):
            return False
        if abs((_d(a['txn_date']) - _d(b['txn_date'])).days) > 180:
            return False
        return abs(abs(a['amount']) - abs(b['amount'])) <= max(1.0, abs(b['amount']) * .1)

    groups = {'open': [], 'learned': []}
    for candidate in candidates:
        key = candidate.get('dedupe_key')
        if key in decisions:
            continue
        matches = [(r, d) for r, d, snap in examples if comparable(candidate, r, snap)] if key not in exceptions else []
        # A nearby confirmed issue vetoes normal learning in this region.
        expected = [(r, d) for r, d in matches if d['outcome'] == 'expected']
        if expected and not any(d['outcome'] == 'issue' for _, d in matches):
            source, _ = min(expected, key=lambda pair: (abs(abs(pair[0]['amount'])-abs(candidate['amount'])), abs((_d(pair[0]['txn_date'])-_d(candidate['txn_date'])).days)))
            groups['learned'].append({**candidate, 'outcome': 'learned', 'learning_source_id': source['id'],
                'learning_reason': f"Similar to the £{abs(source['amount']):,.2f} payment on {source['txn_date']} you marked expected. Same merchant, account, category, payment context and flag type; within 10% (minimum £1) and 180 days. This inference has not been manually confirmed."})
        else:
            groups['open'].append(candidate)
    return groups


# ----- savings advice ----------------------------------------------------------

def savings_advice(conn):
    rec = detect_recurring(conn)
    tips = []
    subs = [r for r in rec if semantic(r) == "Subscriptions" and r["status"] != "ended" and not r["data_stale"] and r.get("forecast_eligible",True)]
    if subs:
        total = sum(s["monthly_cost"] for s in subs)
        tips.append({
            "type": "subscriptions",
            "title": f"{len(subs)} active subscriptions cost ~£{total:.0f}/mo",
            "detail": "Review and cancel ones you no longer use.",
            "items": [{"merchant": s["merchant"], "monthly": s["monthly_cost"], "account_id":s["account_id"], "txn_ids":s["txn_ids"]} for s in subs],
            "potential_monthly_saving": round(total, 2),
        })
    price_changes = [r for r in rec if r["status"] == "price_change" and not r["data_stale"] and r.get("forecast_eligible",True)]
    for p in price_changes:
        tips.append({
            "type": "price_change",
            "title": f"{p['merchant']} cost has changed",
            "account_id":p["account_id"],"txn_ids":p["txn_ids"],"merchant":p["merchant"],
            "detail": f"Recent charges differ from the usual £{p['typical_amount']:.2f}.",
            "potential_monthly_saving": 0,
        })
    # fees paid — amortised over the ACTUAL number of months the data spans
    # (not a hardcoded constant), to match the rest of the module's monthly figures.
    fees = sum(-r['amount'] for r in cashflow_rows(conn)
               if semantic(r) == 'Fees & interest' and (r['amount'] < 0 or r['is_refund']))
    if fees and fees > 0:
        n_months = len(monthly_cashflow(conn)) or 1
        tips.append({
            "type": "fees",
            "title": f"You paid £{fees:.2f} in fees & interest",
            "detail": "Review the underlying charges: some may be avoidable; savings are not guaranteed.",
            "potential_monthly_saving": round(fees / max(n_months, 1), 2),
        })
    return tips


# ----- analytics bundle (dashboard granularity) -------------------------------

# Household cash-flow excludes internal moves: matched transfers, transfer/deposit
# rows, and investment buys/sells (which churn the portfolio, not the budget).
EXCLUDE_CATS = ("Investments", "Transfers & payments")
CASHFLOW = ("txn_date IS NOT NULL AND transfer_group IS NULL"
            " AND COALESCE(spending_treatment,'normal') != 'self_transfer'"
            " AND COALESCE(exclude_from_analysis,0)=0"
            " AND COALESCE(category,'') NOT IN ('Investments','Transfers & payments')")


def _is_refund(row):
    if row["amount"] <= 0:
        return False
    if row.get("spending_treatment") == "reimbursement":
        return True
    role = row.get('category_treatment')
    if role == 'refund':
        return True
    if row.get("merchant_kind") == "person" or semantic(row) == "Friends & shared costs":
        return False
    if re.search(r"refund|reversal|reversed|chargeback", row["description"] or "", re.I):
        return True
    if role == 'income' and row['account_type'] != 'credit':
        return False
    if role is None and semantic(row) in {"Income", "Investment income"} and row["account_type"] != "credit":
        return False
    return row["account_type"] == "credit" or (semantic(row) not in {None, "", "Uncategorized", "Cash"}
        and (role is not None or semantic(row) not in {"Income", "Investment income"}))


def _exclusion_reason(row):
    if row.get('account_excluded'):
        return 'Excluded account'
    if row.get('spending_treatment') == 'self_transfer' and _exchange_details(row)[0]:
        return "Currency conversions"
    if row.get("spending_treatment") == "self_transfer":
        return "Self transfers"
    if row["transfer_group"]:
        return "Matched account transfers"
    role = row.get('category_treatment')
    if role in {'transfer', 'investment'} or (role is None and semantic(row) in EXCLUDE_CATS):
        return "Investment trades" if role == 'investment' or semantic(row) == 'Investments' else "Transfers and repayments"
    if row.get("exclude_from_analysis"):
        return "User exclusions"
    return None


def _exchange_details(row):
    try:
        meta = json.loads(row.get('meta') or '{}')
        classification = json.loads(row.get('classification') or '{}')
    except (TypeError, ValueError):
        return False, 0.0
    if not isinstance(meta, dict) or not isinstance(classification, dict):
        return False, 0.0
    exchange = bool(meta.get('currency_exchange') or classification.get('movement_type') == 'currency_exchange')
    fee = meta.get('exchange_fee_gbp')
    valid_fee = (exchange and row['amount'] < 0 and isinstance(fee, (int, float))
                 and not isinstance(fee, bool) and math.isfinite(fee) and 0 <= fee <= -row['amount'])
    return exchange, round(fee, 2) if valid_fee else 0.0


def _household_component(row):
    reason = _exclusion_reason(row)
    if reason is None:
        component = {**row, 'is_refund': _is_refund(row)}
        if row.get('account_ownership_share', 1) != 1:
            component.update(original_amount=row['amount'], amount=round(row['amount'] * row['account_ownership_share'], 2),
                             analysis_component='Configured household ownership share')
        return component
    if reason == 'Currency conversions' and not row.get('exclude_from_analysis'):
        _, fee = _exchange_details(row)
        if fee:
            # A source exchange row can contain a printed fee as well as a
            # currency move. Split only the analytical contribution; the stored
            # amount, original transaction ID and account balance are untouched.
            return {**row, 'amount': round(-fee * row.get('account_ownership_share', 1), 2), 'original_amount': row['amount'],
                    'category': 'Fees & interest', 'category_semantic': 'Fees & interest', 'category_treatment': 'normal', 'category_id': None,
                    'spending_treatment': 'normal',
                    'transfer_group': None, 'is_refund': False,
                    'analysis_component': 'Printed currency exchange fee'}
    return None


def _filtered_rows(conn, account=None, date_from=None, date_to=None, *, source_rows=None):
    for value in (date_from, date_to):
        if value and (not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) or _d(value) is None):
            raise ValueError("Dates must be valid YYYY-MM-DD dates")
    if date_from and date_to and date_from > date_to:
        raise ValueError("Start date must not follow end date")
    return [r for r in (source_rows if source_rows is not None else _rows(conn))
            if (account is None or r["account_id"] == account)
            and (date_from is None or r["txn_date"][:10] >= date_from)
            and (date_to is None or r["txn_date"][:10] <= date_to)]


def household_cashflow_rows(conn, account=None, date_from=None, date_to=None,
                            exclude_outliers=False, outlier_z=3.5):
    """Signed household movements, with refunds distinguished from earnings."""
    flagged = {r["id"] for r in detect_anomalies(conn, z=outlier_z, include_resolved=True) if r.get('kind') == 'large_amount'} if exclude_outliers else set()
    components = [component for r in _filtered_rows(conn, account, date_from, date_to)
            if (component := _household_component(r)) is not None and r['id'] not in flagged]
    _, catalog = category_maps(conn)
    for component in components:
        category = catalog.get((component.get('category') or '').casefold())
        if category:
            component['category'] = category['name']
            annotate_category(component, catalog)
    return components


cashflow_rows = household_cashflow_rows


def monthly_cashflow(conn, account=None, interval="month", date_from=None, date_to=None,
                     exclude_outliers=False, outlier_z=3.5):
    """Continuous calendar months; spend is gross purchases less refunds.

    Empty months inside the observed date span are explicit zeroes, not evidence
    of complete coverage. Consumers must inspect statement coverage separately.
    """
    if interval not in {"month", "day"}:
        raise ValueError("interval must be month or day")
    movements = cashflow_rows(conn, account=account, date_from=date_from, date_to=date_to,
                              exclude_outliers=exclude_outliers, outlier_z=outlier_z)
    return _aggregate_months(movements, interval=interval)


def _aggregate_months(movements, interval="month", first=None, last=None):
    monthly = {}
    for r in movements:
        key = r["txn_date"][:7] if interval == "month" else r["txn_date"][:10]
        m = monthly.setdefault(key, {"m": key, "income": 0.0, "gross_spend": 0.0,
                                     "refunds": 0.0, "investment_income": 0.0})
        if r["amount"] < 0:
            m["gross_spend"] -= r["amount"]
        elif r["is_refund"]:
            m["refunds"] += r["amount"]
        else:
            m["income"] += r["amount"]
            if semantic(r) == "Investment income" or r["account_type"] == "investment":
                m["investment_income"] += r["amount"]
    if not monthly and not (first and last):
        return []
    first, last = first or min(monthly), last or max(monthly)
    if interval == "month":
        first_year, first_month = map(int, first[:7].split("-"))
        last_year, last_month = map(int, last[:7].split("-"))
        # Numeric bounds keep the final supported calendar year finite. A
        # lexical loop would consider "10000-01" earlier than "9999-12".
        for index in range(first_year * 12 + first_month - 1, last_year * 12 + last_month):
            year, month = divmod(index, 12)
            key = f"{year:04d}-{month + 1:02d}"
            monthly.setdefault(key, {"m": key, "income": 0.0, "gross_spend": 0.0,
                                      "refunds": 0.0, "investment_income": 0.0})
    out = []
    for key in sorted(monthly):
        m = monthly[key]
        m["spend"] = m["gross_spend"] - m["refunds"]
        m["net"] = m["income"] - m["spend"]
        out.append({k: round(v, 2) if isinstance(v, float) else v for k, v in m.items()})
    return out


def month_coverage(conn, months, account=None, date_from=None, date_to=None, today=None):
    """Continuous reconciled bank/card coverage, separate from observed totals."""
    today = today or date.today()
    from . import account_coverage
    required, intervals, gaps, sources = account_coverage.state(conn, planning=False, account_id=account)

    result = {}
    for month in months:
        year, number = map(int, month.split("-"))
        start = date(year, number, 1)
        end = date(year, number, calendar.monthrange(year, number)[1])
        missing, required_count = account_coverage.missing(required, intervals, gaps, start, end)
        filtered = bool((date_from and date_from > start.isoformat()) or (date_to and date_to < end.isoformat()))
        in_progress = end >= today
        complete = bool(required_count) and not missing and not filtered and not in_progress
        label = ("Partial date selection" if filtered else "Month in progress" if in_progress
                 else ("Confirmed account coverage" if 'user_confirmed' in sources else "Verified bank/card coverage") if complete else "Incomplete statement coverage")
        result[month] = {"coverage_complete": complete, "coverage_label": label,
                         "missing_accounts": missing, "partial_selection": filtered,
                         "month_in_progress": in_progress, "coverage_sources": sources}
    return result


def analytics_bundle(conn, top_n=12, top_categories=7, *, account=None, date_from=None,
                     date_to=None, exclude_outliers=False, outlier_z=3.5, today=None):
    """Household cash flow, excluding internal moves and netting refunds."""
    source_rows = _rows(conn)
    selected = _filtered_rows(conn, account, date_from, date_to, source_rows=source_rows)
    all_movements = [component for r in source_rows if (component := _household_component(r)) is not None]
    selected_ids = {r["id"] for r in selected}
    recorded = [r for r in all_movements if r["id"] in selected_ids]
    flags = {r["id"]: r for r in detect_anomalies(conn, z=outlier_z, include_resolved=True,
             spending_rows=[r for r in all_movements if r["amount"] < 0]) if r.get('kind') == 'large_amount'}
    removed = [r for r in recorded if exclude_outliers and r["id"] in flags]
    movements = [r for r in recorded if not exclude_outliers or r["id"] not in flags]
    observed = recorded or selected
    first = date_from[:7] if date_from else min((r["txn_date"][:7] for r in observed), default=None)
    last = date_to[:7] if date_to else max((r["txn_date"][:7] for r in observed), default=None)
    if first and last:
        distance = (int(last[:4])-int(first[:4]))*12 + int(last[5:])-int(first[5:])
        if distance > 1200:
            raise ValueError("Select a period of 100 years or less")
    months = _aggregate_months(movements, first=first, last=last)
    actual_months = {m["m"]: m for m in _aggregate_months(recorded, first=first, last=last)}
    coverage = month_coverage(conn, [m["m"] for m in months], account, date_from, date_to, today)
    movement_counts = Counter(r["txn_date"][:7] for r in movements)
    observed_months = {r["txn_date"][:7] for r in selected}
    investment_spending_months = {r["txn_date"][:7] for r in recorded if r["account_type"] == "investment" and r["amount"] < 0}
    for month in months:
        month.update(coverage[month["m"]])
        month["recorded_spend"] = actual_months[month["m"]]["spend"]
        month["excluded_outlier_spend"] = round(month["recorded_spend"]-month["spend"], 2)
        month["transaction_count"] = movement_counts[month["m"]]
        month["has_observations"] = month["m"] in observed_months
        month["investment_export_spending"] = month["m"] in investment_spending_months
    n_months = len(months)
    tot_income = sum(m["income"] for m in months)
    tot_spend = sum(m["spend"] for m in months)
    busiest = max(months, key=lambda m: m["spend"], default=None)
    merchants, accounts, cats = {}, {}, defaultdict(float)
    names = {r["id"]: r["display_name"] for r in conn.execute("SELECT id, display_name FROM accounts")}
    for r in movements:
        expense = -r["amount"] if r["amount"] < 0 or r["is_refund"] else 0.0
        account = accounts.setdefault(r["account_id"], {"account": names[r["account_id"]],
                                                     "spend": 0.0, "income": 0.0, "n": 0})
        account["n"] += 1
        account["spend"] += expense
        if r["amount"] > 0 and not r["is_refund"]:
            account["income"] += r["amount"]
        if expense:
            name = r["merchant"] or r["description"] or "Unknown merchant"
            merchant = merchants.setdefault(name, {"merchant": name, "spend": 0.0, "n": 0})
            merchant["spend"] += expense
            merchant["n"] += 1
            cats[r["category"] or "Uncategorized"] += expense
    top_merchants = sorted(merchants.values(), key=lambda r: r["spend"], reverse=True)[:top_n]
    by_account = sorted(accounts.values(), key=lambda r: r["spend"], reverse=True)
    for r in top_merchants + by_account:
        r["spend"] = round(r["spend"], 2)
        if "income" in r:
            r["income"] = round(r["income"], 2)
    biggest = min((r for r in movements if r["amount"] < 0), key=lambda r: r["amount"], default=None)
    big = {k: biggest[k] for k in ("txn_date", "description", "merchant")} if biggest else None
    if big:
        big["amount"] = round(-biggest["amount"], 2)
    keep = sorted(cats, key=lambda c: (-cats[c], c))[:top_categories]
    labels = keep + ([] if "Other" in keep else ["Other"])
    keys = [m["m"] for m in months]
    matrix = {c: {m: 0.0 for m in keys} for c in labels}
    for r in movements:
        if r["amount"] < 0 or r["is_refund"]:
            cat = r["category"] or "Uncategorized"
            matrix[cat if cat in keep else "Other"][r["txn_date"][:7]] -= r["amount"]
    series = [{"name": c, "data": [round(matrix[c][m], 2) for m in keys]}
              for c in labels if any(matrix[c].values())]
    complete = [m for m in months if m["coverage_complete"]]
    exclusion_groups = {}

    def add_exclusion(row, reason):
        if reason:
            item = exclusion_groups.setdefault(reason, {"reason": reason, "count": 0, "outflow": 0.0, "inflow": 0.0, "net_spending_impact": 0.0, "income_impact": 0.0})
            item["count"] += 1
            item["outflow"] += max(-row["amount"], 0)
            item["inflow"] += max(row["amount"], 0)
            # Transfers and trades have no household-budget impact. Only the
            # optional exclusions remove otherwise eligible spending/receipts.
            if reason in {"User exclusions", "Outlier filter"}:
                item["net_spending_impact"] += -row["amount"] if row["amount"] < 0 or _is_refund(row) else 0
                item["income_impact"] += row["amount"] if row["amount"] > 0 and not _is_refund(row) else 0
    for row in selected:
        reason = _exclusion_reason(row)
        if reason == 'Currency conversions':
            _, fee = _exchange_details(row)
            add_exclusion({**row, 'amount': row['amount'] + fee}, reason)
            if fee and (row.get('exclude_from_analysis') or (exclude_outliers and row['id'] in flags)):
                add_exclusion({**row, 'amount': -fee, 'category': 'Fees & interest'},
                              'User exclusions' if row.get('exclude_from_analysis') else 'Outlier filter')
        else:
            if not reason and exclude_outliers and row["id"] in flags:
                reason = "Outlier filter"
            add_exclusion(row, reason)
    exclusions = [{k: round(v, 2) if isinstance(v, float) else v for k, v in group.items()} for group in exclusion_groups.values()]
    current, previous = (months[-1] if months else None), (months[-2] if len(months) > 1 else None)
    change = round(current["spend"]-previous["spend"], 2) if previous else None
    comparison = {"current_month": current["m"] if current else None, "previous_month": previous["m"] if previous else None,
                  "current_spend": current["spend"] if current else None, "previous_spend": previous["spend"] if previous else None,
                  "change_amount": change, "change_pct": round(change/previous["spend"]*100, 1) if previous and previous["spend"] > 0 else None,
                  "comparable": bool(previous and current["coverage_complete"] and previous["coverage_complete"]),
                  "explanation": "Latest two selected calendar months. A percentage is shown only when previous net spending is positive. Compare complete months; partial months have fewer observed days."}
    drivers = []
    if previous:
        now_cats, prev_cats = defaultdict(float), defaultdict(float)
        for row in movements:
            expense = -row["amount"] if row["amount"] < 0 or row["is_refund"] else 0
            month = row["txn_date"][:7]
            if month == current["m"]:
                now_cats[row["category"] or "Uncategorized"] += expense
            elif month == previous["m"]:
                prev_cats[row["category"] or "Uncategorized"] += expense
        for category in sorted(set(now_cats) | set(prev_cats)):
            delta = now_cats[category]-prev_cats[category]
            drivers.append({"category": category, "spend": round(now_cats[category], 2), "previous_spend": round(prev_cats[category], 2),
                            "change_amount": round(delta, 2), "change_pct": round(delta/prev_cats[category]*100, 1) if prev_cats[category] > 0 else None})
        drivers.sort(key=lambda r: abs(r["change_amount"]), reverse=True)
    insights = []
    if any(_exclusion_reason(r) == 'Currency conversions' for r in selected):
        insights.append({'severity': 'warning', 'title': 'Converted foreign cash is outside this GBP view',
                         'detail': 'Currency conversion principal is a movement of your own money. Only explicitly printed GBP conversion fees count as spending. Later purchases from the foreign-currency balance are missing until that statement is imported; the conversion is not a substitute for those purchases.'})
    if len(complete) != n_months:
        insights.append({"severity": "warning", "title": "Some months have incomplete coverage", "detail": f"{len(complete)} of {n_months} selected months have continuous reconciled bank/card statements. Missing months do not establish zero spending. Typical spending uses only the verified months."})
    if any(m["investment_export_spending"] for m in months):
        insights.append({"severity": "warning", "title": "Broker card spending has export coverage", "detail": "Broker card purchases count in spending, but the broker export has no independently verified statement coverage. Verified month labels refer to bank/card accounts."})
    if removed:
        insights.append({"severity": "info", "title": "Outlier filter is active", "detail": f"{len(removed)} flagged charges totalling £{sum(-r['amount'] for r in removed):,.2f} are removed from this view. Recorded totals remain visible. These can be legitimate costs, so this adjusted view is not additional money available to save."})
    if comparison["comparable"] and change is not None:
        insights.append({"severity": "info", "title": "Monthly spending change", "detail": f"Net spending {'rose' if change >= 0 else 'fell'} by £{abs(change):,.2f} between {previous['m']} and {current['m']}. Category changes below sum to this difference."})
    definitions = {
        "spending": "Negative household transactions minus recognised positive refunds/reimbursements. Self transfers, matched transfers, repayments, investment trades and user exclusions are removed. Friends' payments remain until you classify them.",
        "income": "Positive household cash receipts excluding recognised refunds/reimbursements and internal transfers. This can include gifts or other non-salary receipts.",
        "net": "Income minus net spending. It is the change from included cash flows, not an independently reconciled account balance.",
        "typical_monthly_spend": "Median net spending among fully selected, completed months with continuous reconciled bank/card statements. No value is shown without an eligible month. Broker card spending relies on export coverage.",
        "avg_monthly_spend": "Total selected net spending divided by all selected calendar months. Partial months and missing statements may make this average misleading; use coverage labels.",
        "savings_rate": "(Included income minus net spending) divided by included income. Undefined when income is zero; this is a recorded cash-flow ratio, not investment performance.",
        "outliers": "Banking amount flags use strictly earlier dates within 365 days on the same account and payment context. Merchant comparisons require five earlier payments; broad category comparisons need at least twenty across five merchants and exclude rent, bills, subscriptions, person payments, cash and uncategorized rows. The threshold combines median/MAD with absolute and relative floors. Duplicate evidence remains in spending unless independently flagged for amount or explicitly excluded. Date filters never redefine the earlier baseline. Investment flag behavior is retained separately.",
        "exclusions": "Outflow and inflow show both sides separately; moving £100 between your accounts is £100 out plus £100 in, not £200 of spending. Currency conversions show principal separately from any explicitly printed GBP fee, which counts as spending; a separately excluded fee can appear in a second reason. Impact fields quantify optional user/outlier exclusions. No stored transaction is deleted by these filters.",
        "category_drivers": "Net spending in each category in the latest selected month minus the preceding month. Refunds can produce negative category spending. Category changes sum to the monthly change.",
    }
    return {
        "stats": {"months": n_months,
                  "total_income": round(tot_income, 2), "total_spend": round(tot_spend, 2),
                  "total_net": round(tot_income-tot_spend, 2),
                  "gross_spend": round(sum(m["gross_spend"] for m in months), 2),
                  "recorded_spend": round(sum(m["spend"] for m in actual_months.values()), 2),
                  "median_monthly_spend": round(statistics.median(m["spend"] for m in months), 2) if months else None,
                  "typical_monthly_spend": round(statistics.median(m["spend"] for m in complete), 2) if complete else None,
                  "complete_months": len(complete),
                  "excluded_outlier_count": len(removed), "excluded_outlier_spend": round(sum(-r["amount"] for r in removed), 2),
                  "outlier_candidates": sum(r["id"] in flags for r in recorded),
                  "avg_monthly_income": round(tot_income / n_months, 2) if n_months else 0,
                  "avg_monthly_spend": round(tot_spend / n_months, 2) if n_months else 0,
                  "avg_monthly_net": round((tot_income - tot_spend) / n_months, 2) if n_months else 0,
                  "savings_rate": round((tot_income - tot_spend) / tot_income, 3) if tot_income else None,
                  "busiest_month": busiest["m"] if busiest else None,
                  "busiest_month_spend": busiest["spend"] if busiest else 0,
                  "biggest_expense": big,
                  "refunds": round(sum(m["refunds"] for m in months), 2),
                  "averaging_method": "Calendar months between first and last cash-flow observation; partial and missing statement months may be included."},
        "top_merchants": top_merchants, "by_account": by_account,
        "category_months": {"months": keys, "series": series},
        "monthly": months, "comparison": comparison, "drivers": drivers,
        "category_totals": [{"category": c, "spend": round(cats[c], 2)} for c in sorted(cats, key=lambda c: -cats[c])],
        "exclusions": exclusions, "definitions": definitions, "insights": insights,
        "filters": {"account": account, "date_from": date_from, "date_to": date_to,
                    "exclude_outliers": exclude_outliers, "outlier_z": outlier_z},
    }


# ----- investments / trading analytics ----------------------------------------

BUY_ACTIONS = ("Market buy", "Limit buy", "Stop buy", "Stop limit buy")
SELL_ACTIONS = ("Market sell", "Limit sell", "Stop sell", "Stop limit sell")


def investments_bundle(conn, account_ids=None):
    """Cash history with exact GBP and dated-reference estimates kept separate.

    Lifetime purchases include sold positions. They must not be confused with
    the cost of shares still held or today's securities value.
    """
    from .portfolio import ZERO, decimal, major_currency
    from .investment_accounts import available_accounts, selected_accounts, scope_sql
    available = available_accounts(conn)
    if not available:
        return None
    account_ids = selected_accounts(conn, account_ids)
    accounts = [a for a in available if a['id'] in account_ids]
    scope, params = scope_sql(account_ids)
    rows = [r for r in _rows(conn) if r["account_type"] == "investment" and r['account_id'] in account_ids]
    reference = {(r['currency'], r['date']): r['rate_to_gbp']
                 for r in conn.execute('SELECT currency,date,rate_to_gbp FROM reference_fx')}
    metrics = ('deposited', 'withdrawn', 'invested', 'divested', 'dividends', 'interest', 'cashback', 'result_adjustments')

    def bucket():
        return {key: {'known': ZERO, 'estimated': ZERO, 'complete': True,
                      'estimated_complete': True, 'estimated_count': 0} for key in metrics}

    def add(target, key, exact, estimate):
        if exact is None:
            target[key]['complete'] = False
        else:
            target[key]['known'] += exact
        if estimate is None:
            target[key]['estimated_complete'] = False
        else:
            target[key]['estimated'] += estimate
            target[key]['estimated_count'] += int(exact is None)

    def finish(target):
        result = {}
        for key, item in target.items():
            result[key] = round(float(item['known']), 2) if item['complete'] else None
            result['known_' + key] = round(float(item['known']), 2)
            result['estimated_' + key] = round(float(item['estimated']), 2) if item['estimated_complete'] else None
            result['estimated_' + key + '_count'] = item['estimated_count']
        for prefix in ('', 'known_', 'estimated_'):
            for key, parts in (('net_contributed', (('deposited', 1), ('withdrawn', -1))),
                               ('net_invested', (('invested', 1), ('divested', -1))),
                               ('income', (('dividends', 1), ('interest', 1), ('cashback', 1)))):
                result[prefix + key] = (round(sum(result[prefix + part] * sign for part, sign in parts), 2)
                                        if all(result[prefix + part] is not None for part, _ in parts) else None)
        return result

    totals, monthly, holdings, actions, currencies = bucket(), {}, {}, {}, {}
    cash_history_complete = True
    for row in rows:
        try:
            meta = json.loads(row['meta'] or '{}')
        except (TypeError, ValueError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        action = row['type_code'] or 'Other'
        currency, scale = major_currency(meta.get('native_currency') or meta.get('currency') or 'GBP')
        native = decimal(meta.get('native_amount'))
        native = native * scale if native is not None else (decimal(row['amount']) if currency == 'GBP' else None)
        known = bool(meta.get('gbp_value_known', currency == 'GBP'))
        exact = decimal(row['amount']) if known else None
        rate = decimal(reference.get((currency, row['txn_date'])))
        estimate = exact if exact is not None else (native * rate if native is not None and rate is not None and rate > 0 else None)
        if action != 'Currency conversion':
            cash_history_complete &= known
        metric = ('invested' if action in BUY_ACTIONS else 'divested' if action in SELL_ACTIONS
                  else 'deposited' if action == 'Deposit' else 'withdrawn' if action == 'Withdrawal'
                  else 'dividends' if action.startswith('Dividend') else 'interest' if action in ('Interest on cash', 'Lending interest')
                  else 'cashback' if action == 'Spending cashback' else 'result_adjustments' if action == 'Result adjustment' else None)
        sign = -1 if metric in {'invested', 'withdrawn'} else 1
        month = row['txn_date'][:7]
        timeline = monthly.setdefault(month, bucket())
        if metric:
            signed_exact = exact * sign if exact is not None else None
            signed_estimate = estimate * sign if estimate is not None else None
            add(totals, metric, signed_exact, signed_estimate)
            add(timeline, metric, signed_exact, signed_estimate)
        if action in BUY_ACTIONS + SELL_ACTIONS:
            ticker = row['merchant'] or '—'
            h = holdings.setdefault(ticker, {'ticker': ticker, 'trades': 0, 'last': None, 'values': bucket()})
            add(h['values'], metric, signed_exact, signed_estimate)
            h['trades'] += 1
            h['last'] = max(h['last'] or '', row['txn_date'])
        by_action = actions.setdefault(action, {'action': action, 'n': 0, 'total': ZERO, 'complete': True, 'native': defaultdict(lambda: ZERO)})
        by_action['n'] += 1
        by_action['total'] += decimal(row['amount']) or ZERO
        by_action['complete'] &= known
        movements = meta.get('cash_movements')
        if not isinstance(movements, dict):
            movements = {currency: native / scale} if native is not None else {}
        for cash_currency, movement in movements.items():
            cash_currency, cash_scale = major_currency(cash_currency)
            amount = decimal(movement)
            if amount is None or not cash_currency:
                continue
            amount *= cash_scale
            by_action['native'][cash_currency] += amount
            group = currencies.setdefault(cash_currency, {'currency': cash_currency, 'values': bucket(), 'cash_balance': ZERO})
            group['cash_balance'] += amount
            if metric:
                add(group['values'], metric, amount * sign, amount * sign)

    stats = finish(totals)
    # The ledger cash balance includes undated source rows. It remains actual
    # GBP cash; reference conversions never create or change a cash movement.
    stats['cash_balance'] = conn.execute(
        "SELECT ROUND(COALESCE(SUM(t.amount),0),2) FROM transactions t"
        " JOIN accounts a ON a.id=t.account_id WHERE a.account_type='investment'" + scope, params).fetchone()[0]
    stats.update(cash_history_complete=cash_history_complete,
        history_note=("Some activity was settled in foreign currency. GBP estimates use dated ECB reference FX; "
                      "native-currency totals and the actual GBP cash ledger remain separate.") if not cash_history_complete else None,
        n_trades=sum(h['trades'] for h in holdings.values()),
        n_holdings=sum(p['net_shares'] > 0 for p in _holding_positions(conn, account_ids).values()),
        n_historical_tickers=len(holdings))
    top_holdings = []
    for h in holdings.values():
        values = finish(h.pop('values'))
        h.update(bought=values['invested'], sold=values['divested'], net=values['net_invested'],
                 estimated_bought=values['estimated_invested'], estimated_sold=values['estimated_divested'],
                 estimated_net=values['estimated_net_invested'], known_net=values['known_net_invested'],
                 cash_history_complete=values['invested'] is not None and values['divested'] is not None)
        top_holdings.append(h)
    top_holdings.sort(key=lambda h: h['estimated_net'] if h['estimated_net'] is not None else h['known_net'], reverse=True)
    timeline = []
    for month, values in sorted(monthly.items()):
        values = finish(values)
        timeline.append({'month': month, 'bought': values['known_invested'], 'sold': values['known_divested'],
                         'income': values['known_income'], 'estimated_bought': values['estimated_invested'],
                         'estimated_sold': values['estimated_divested'], 'estimated_income': values['estimated_income'],
                         'cash_history_complete': all(values[k] is not None for k in ('invested', 'divested', 'income'))})
    native_totals = []
    native_balances = _investment_native_balances(conn, account_ids)
    for currency in native_balances:
        currencies.setdefault(currency, {'currency': currency, 'values': bucket(), 'cash_balance': ZERO})
    for currency, group in sorted(currencies.items()):
        values = finish(group['values'])
        native_totals.append({'currency': currency, 'cash_balance': round(native_balances.get(currency, 0), 2),
                              **{key: values[key] for key in (*metrics, 'net_contributed', 'net_invested', 'income')}})
    return {'account': ', '.join(a['display_name'] or a['id'] for a in accounts),
            'accounts': available, 'selected_account_ids': account_ids, 'stats': stats,
            'holdings': top_holdings[:15], 'timeline': timeline,
            'actions': [{'action': a['action'], 'n': a['n'],
                        'total': round(float(a['total']), 2) if a['complete'] else None,
                        'known_total': round(float(a['total']), 2),
                        'native_totals': [{'currency': c, 'amount': round(float(v), 2)} for c, v in sorted(a['native'].items())]}
                        for a in sorted(actions.values(), key=lambda a: (-a['n'], a['action']))],
            'timeline_currency': 'GBP cash movements only; estimated fields include dated reference FX',
            'native_currency_totals': native_totals, 'cash_history_complete': cash_history_complete}


# ----- portfolio valuation: realised + unrealised P/L -------------------------

def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _holding_positions(conn, account_ids=None) -> dict[str, dict]:
    from .portfolio import holding_positions
    return holding_positions(conn, account_ids)


def _cached_prices(conn) -> dict[str, dict]:
    return {r["isin"]: dict(r) for r in conn.execute("SELECT * FROM prices")}


def _utc(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _price_stale(price, now):
    if (price or {}).get('identity_verified') != 1:
        return True
    updated = _utc((price or {}).get("quote_asof"))
    fx_asof = _utc((price or {}).get('fx_asof'))
    if (price or {}).get('currency') not in ('GBP', 'GBp', 'GBX') and fx_asof is None:
        return True
    if fx_asof and fx_asof > now + timedelta(minutes=1):
        return True
    if updated and fx_asof:
        updated = min(updated, fx_asof)
    return updated is None or now - updated > PRICE_TTL or updated > now + timedelta(minutes=1)


def _usable_price(price):
    if (price or {}).get('identity_verified') != 1:
        return None
    value = _number((price or {}).get("price_gbp"))
    return value if value is not None and value > 0 else None


def _price_refresh_due(price, now):
    """Cache lifetime is based on retrieval, not the exchange observation.

    A freshly retrieved Friday close remains an old market observation over
    the weekend. It should not cause another provider request on every read.
    """
    if _usable_price(price) is None:
        return True
    fetched = _utc((price or {}).get('fetched_at') or (price or {}).get('updated'))
    return fetched is None or now - fetched > PRICE_TTL or fetched > now + timedelta(minutes=1)


def _position_view(p, pr, now):
    from .portfolio import major_currency
    closed = p['net_shares'] <= 0
    price_gbp = _usable_price(pr)
    mv = 0.0 if closed else (round(price_gbp * p['net_shares'], 2) if price_gbp is not None else None)
    upl = round(mv - p['cost_basis'], 2) if mv is not None and p['cost_basis'] is not None and p['position_complete'] else None
    currency, scale = major_currency(pr.get('currency'))
    native_price = _number(pr.get('price_native'))
    native_price = native_price * float(scale) if native_price is not None and price_gbp is not None and currency == p['native_currency'] else None
    native_mv = 0.0 if closed else (round(native_price * p['net_shares'], 2) if native_price is not None else None)
    native_upl = round(native_mv - p['native_cost_basis'], 2) if native_mv is not None and p['native_cost_basis'] is not None and p['position_complete'] else None
    estimated_upl = round(mv - p['estimated_cost_basis'], 2) if mv is not None and p['estimated_cost_basis'] is not None and p['position_complete'] else None
    result = {
        'ticker': p['ticker'], 'isin': p['isin'], 'symbol': pr.get('symbol'),
        'account_ids': p.get('account_ids', []),
        'history_complete': p.get('history_complete', True), 'snapshot_sources': p.get('snapshot_sources', []),
        'shares': p['net_shares'], 'avg_cost': p['avg_cost'], 'cost_basis': p['cost_basis'],
        'cost_basis_complete': p['cost_basis_complete'], 'position_complete': p['position_complete'],
        'price': price_gbp, 'corporate_actions': p.get('corporate_actions', []),
        'position_basis': ('broker_statement_snapshot' if p.get('snapshot_sources') else
                           'trade_history_with_corporate_actions' if p.get('corporate_actions') else 'trade_history'),
        'currency': pr.get('currency'), 'market_value': mv, 'unrealised': upl,
        'unrealised_pct': round(upl / p['cost_basis'] * 100, 1) if upl is not None and p['cost_basis'] else None,
        'realised': p['realised'], 'priced': mv is not None,
        'stale': _price_stale(pr, now) if not closed else False,
        'asof': pr.get('quote_asof'),
        'first_trade': p['first'], 'last_trade': p['last'],
        'native_price': native_price, 'native_market_value': native_mv, 'native_unrealised': native_upl,
        'missing_gbp_trade_count': len(p['missing_gbp_trade_ids']),
        'estimated_unrealised': estimated_upl,
        'estimated_unrealised_pct': round(estimated_upl / p['estimated_cost_basis'] * 100, 1) if estimated_upl is not None and p['estimated_cost_basis'] else None,
        'book_method': 'Chronological moving-average cost including fees in the source Total; broker Result shown separately.',
    }
    for key in ('trades', 'bought_shares', 'sold_shares', 'corporate_action_shares', 'gift_shares', 'bought', 'sold',
                'bought_known', 'sold_known', 'native_currency', 'native_bought', 'native_sold',
                'native_cost_basis', 'native_avg_cost', 'native_realised', 'reported_results', 'warnings'):
        result[key] = p[key]
    for key in ('estimated_cost_basis', 'estimated_avg_cost', 'estimated_realised', 'estimated_trade_count',
                'estimated_bought', 'estimated_sold', 'reference_fx'):
        result[key] = p[key]
    for key in ('quote_asof', 'fetched_at', 'fx_asof', 'fx_rate_to_gbp', 'provider', 'source_url', 'identity_verified'):
        result[key] = pr.get(key)
    return result


def _native_totals(positions, prices, now):
    groups = {}
    for p in positions.values():
        currency = p['native_currency']
        if not currency:
            continue
        item = groups.setdefault(currency, {'currency': currency, 'cost_basis': 0.0, 'market_value': 0.0,
            'unrealised': 0.0, 'realised': 0.0, 'n_holdings': 0, 'n_unknown_cost': 0, 'n_unpriced': 0,
            'realised_complete': True})
        if p['native_realised'] is None:
            item['realised_complete'] = False
        else:
            item['realised'] += p['native_realised']
        if p['net_shares'] <= 0:
            continue
        item['n_holdings'] += 1
        row = _position_view(p, prices.get(p['isin']) or {}, now)
        if p['native_cost_basis'] is None:
            item['n_unknown_cost'] += 1
        else:
            item['cost_basis'] += p['native_cost_basis']
        if row['native_market_value'] is None:
            item['n_unpriced'] += 1
        else:
            item['market_value'] += row['native_market_value']
        if row['native_unrealised'] is not None:
            item['unrealised'] += row['native_unrealised']
    for item in groups.values():
        for key in ('cost_basis', 'market_value', 'unrealised', 'realised'):
            item['known_' + key] = round(item[key], 2)
            item[key] = round(item[key], 2)
        if item['n_unknown_cost']:
            item['cost_basis'] = None
        if item['n_unpriced']:
            item['market_value'] = None
        if item['n_unpriced'] or item['n_unknown_cost']:
            item['unrealised'] = None
        if not item['realised_complete']:
            item['realised'] = None
    return list(sorted(groups.values(), key=lambda item: item['currency']))


def _valuation_totals(positions, prices, now):
    market_value = unrealised = priced_cost = total_cost = 0.0
    n_priced = n_unpriced = n_stale = n_unknown_cost = 0
    timestamps = []
    held = [p for p in positions.values() if p["net_shares"] > 0]
    for p in held:
        price = prices.get(p["isin"])
        value = _usable_price(price)
        if p["cost_basis"] is None:
            n_unknown_cost += 1
        else:
            total_cost += p["cost_basis"]
        if value is None:
            n_unpriced += 1
            continue
        # Aggregate the same rounded holding values displayed in the table.
        # Mixing raw products here with rounded row P/L produces a one-penny
        # contradiction between the headline, table sum and value-minus-cost.
        holding_value = round(value * p["net_shares"], 2)
        market_value += holding_value
        if p["cost_basis"] is not None:
            priced_cost += p["cost_basis"]
            unrealised += round(holding_value - p["cost_basis"], 2)
        n_priced += 1
        n_stale += int(_price_stale(price, now))
        stamp = _utc(price.get('quote_asof'))
        fx_stamp = _utc(price.get('fx_asof'))
        if stamp:
            timestamps.append(min(stamp, fx_stamp) if fx_stamp else stamp)
    realised_complete = all(p["realised_complete"] for p in positions.values())
    position_complete = all(p["position_complete"] for p in positions.values())
    realised = round(sum(p["realised_known"] for p in positions.values()), 2)
    pl_complete = n_unpriced == 0 and n_unknown_cost == 0 and position_complete
    estimated_rows = [_position_view(p, prices.get(p['isin']) or {}, now) for p in held]
    estimated_cost_complete = all(p['estimated_cost_basis'] is not None for p in held)
    estimated_pl_complete = estimated_cost_complete and not n_unpriced and position_complete
    estimated_realised_complete = all(p['estimated_realised_complete'] for p in positions.values())
    estimated_cost = round(sum(p['estimated_cost_basis'] or 0 for p in held), 2)
    estimated_unrealised = round(sum(r['estimated_unrealised'] or 0 for r in estimated_rows), 2)
    estimated_realised = round(sum(p['estimated_realised_known'] for p in positions.values()), 2)
    return {"market_value": round(market_value, 2),
            "cost_basis": round(total_cost, 2) if not n_unknown_cost else None,
            "known_cost_basis": round(total_cost, 2),
            "priced_cost_basis": round(priced_cost, 2),
            "unrealised": round(unrealised, 2) if pl_complete else None,
            "known_unrealised": round(unrealised, 2),
            "unrealised_pct": round(unrealised / total_cost * 100, 1) if pl_complete and total_cost else None,
            "realised": realised if realised_complete else None,
            "known_realised": realised,
            "total_pl": round(unrealised + realised, 2) if pl_complete and realised_complete else None,
            "n_priced": n_priced, "n_unpriced": n_unpriced, "n_stale": n_stale,
            "n_unknown_cost": n_unknown_cost, "position_complete": position_complete,
            "valuation_complete": not n_unpriced and position_complete,
            "valuation_fresh": not n_unpriced and not n_stale and position_complete,
            "cost_basis_complete": not n_unknown_cost,
            "native_totals": _native_totals(positions, prices, now),
            "estimated_cost_basis": estimated_cost if estimated_cost_complete else None,
            "known_estimated_cost_basis": estimated_cost,
            "estimated_unrealised": estimated_unrealised if estimated_pl_complete else None,
            "known_estimated_unrealised": estimated_unrealised,
            "n_unknown_estimated_cost": sum(p['estimated_cost_basis'] is None for p in held),
            "n_unsupported_unrealised": sum(r['estimated_unrealised'] is None for r in estimated_rows),
            "estimated_unrealised_pct": round(estimated_unrealised / estimated_cost * 100, 1) if estimated_pl_complete and estimated_cost else None,
            "estimated_realised": estimated_realised if estimated_realised_complete else None,
            "known_estimated_realised": estimated_realised,
            "estimated_total_pl": round(estimated_unrealised + estimated_realised, 2) if estimated_pl_complete and estimated_realised_complete else None,
            "n_estimated_cost": sum(p['cost_basis'] is None and p['estimated_cost_basis'] is not None for p in held),
            "estimated_bought": round(sum(p['estimated_bought'] or 0 for p in positions.values()), 2) if all(p['estimated_bought_complete'] for p in positions.values()) else None,
            "estimated_sold": round(sum(p['estimated_sold'] or 0 for p in positions.values()), 2) if all(p['estimated_sold_complete'] for p in positions.values()) else None,
            "reference_fx_note": "Historical GBP estimates use ECB reference rates for the trade date (preceding publication on non-business days). They are not the broker's execution FX or a tax calculation.",
            "asof": min(timestamps).isoformat() if timestamps else None,
            "newest_asof": max(timestamps).isoformat() if timestamps else None,
            "cost_basis_method": "Chronological moving-average GBP book cost; not a tax calculation"}


def _investment_native_balances(conn, account_ids=None):
    """Native cash, with authoritative dated snapshots for incomplete histories."""
    from .broker_snapshots import broker_snapshot_status
    from .investment_accounts import scope_sql
    from .portfolio import ZERO, decimal, major_currency
    scope, params = scope_sql(account_ids)
    by_account = defaultdict(lambda: defaultdict(lambda: ZERO))
    for r in conn.execute("SELECT t.account_id,t.amount,t.meta FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE a.account_type='investment'" + scope, params):
        try:
            meta = json.loads(r['meta'] or '{}')
        except (TypeError, ValueError):
            meta = {}
        meta = meta if isinstance(meta, dict) else {}
        movements = meta.get('cash_movements')
        if not isinstance(movements, dict):
            currency = meta.get('native_currency') or meta.get('currency') or 'GBP'
            movements = {currency: meta.get('native_amount', r['amount'] if currency == 'GBP' else None)}
        for currency, amount in movements.items():
            currency, scale = major_currency(currency)
            value = decimal(amount)
            if currency and value is not None:
                by_account[r['account_id']][currency] += value * scale
    for account, status in broker_snapshot_status(conn, account_ids).items():
        if status['use_snapshot_fallback']:
            by_account[account] = {c: decimal(v) or ZERO for c,v in status['cash_balances'].items()}
    balances = defaultdict(lambda: ZERO)
    for cash in by_account.values():
        for currency, amount in cash.items():
            balances[currency] += amount
    return {c:float(v) for c,v in balances.items()}


def _foreign_cash(conn, now=None, account_ids=None):
    balances = _investment_native_balances(conn, account_ids)
    try:
        rates = {r['currency']: dict(r) for r in conn.execute('SELECT * FROM fx_quotes')}
    except sqlite3.OperationalError:
        rates = {}
    now = _utc(now) or datetime.now(timezone.utc)
    result = []
    for currency, amount in sorted(balances.items()):
        if currency == 'GBP' or abs(amount) < .005:
            continue
        row = {'currency': currency, 'amount': round(amount, 2), 'value_gbp': None}
        rate = rates.get(currency) or {}
        multiplier = _number(rate.get('rate_to_gbp'))
        stamp = _utc(rate.get('asof'))
        if multiplier is not None and multiplier > 0 and stamp and stamp <= now + timedelta(minutes=1):
            row.update(value_gbp=round(amount * multiplier, 2), rate_to_gbp=multiplier,
                       asof=rate.get('asof'), source_url=rate.get('source_url'), stale=now-stamp > PRICE_TTL)
        result.append(row)
    return result


def _add_foreign_cash(conn, result, now=None, account_ids=None):
    result['foreign_cash'] = _foreign_cash(conn, now, account_ids)
    result['foreign_cash_unvalued'] = sum(r['value_gbp'] is None for r in result['foreign_cash'])
    result['foreign_cash_stale'] = sum(bool(r.get('stale')) for r in result['foreign_cash'])
    result['foreign_cash_value'] = round(sum(r['value_gbp'] or 0 for r in result['foreign_cash']), 2)


def _refresh_fx(conn, price_api, now, account_ids=None):
    from .investment_accounts import scope_sql
    scope, params = scope_sql(account_ids)
    from .portfolio import major_currency
    try:
        existing = {(r['currency'], r['date']) for r in conn.execute('SELECT currency,date FROM reference_fx')}
    except sqlite3.OperationalError:
        return {'historical_attempted': 0, 'historical_updated': 0, 'historical_failed': 0,
                'cash_attempted': 0, 'cash_updated': 0, 'cash_failed': 0}
    needed = set()
    for row in conn.execute("SELECT t.meta,t.txn_date,t.type_code FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE a.account_type='investment'" + scope, params):
        try:
            meta = json.loads(row['meta'] or '{}')
        except (ValueError, TypeError):
            continue
        if not isinstance(meta, dict):
            continue
        currency, _ = major_currency(meta.get('native_currency'))
        key = (currency, row['txn_date'])
        action = row['type_code'] or ''
        relevant = action in BUY_ACTIONS + SELL_ACTIONS + ('Deposit', 'Withdrawal', 'Interest on cash', 'Lending interest', 'Spending cashback', 'Result adjustment') or action.startswith('Dividend')
        if relevant and currency and currency != 'GBP' and row['txn_date'] and key not in existing:
            needed.add(key)
    try:
        fetched = price_api.batch_reference_fx(sorted(needed)) if needed else {}
    except Exception:
        fetched = {}
    updated = 0
    for key, rate in fetched.items():
        if key not in needed or not isinstance(rate, dict):
            continue
        value = _number(rate.get('rate_to_gbp'))
        if value is None or value <= 0:
            continue
        conn.execute('INSERT INTO reference_fx(date,currency,rate_to_gbp,source,observed_date,fetched_at,source_url) VALUES(?,?,?,?,?,?,?) '
                     'ON CONFLICT(date,currency) DO UPDATE SET rate_to_gbp=excluded.rate_to_gbp,source=excluded.source,observed_date=excluded.observed_date,fetched_at=excluded.fetched_at,source_url=excluded.source_url',
                     (key[1], key[0], value, rate.get('source'), rate.get('observed_date'), rate.get('fetched_at') or now.isoformat(), rate.get('source_url')))
        updated += 1
    cash = _foreign_cash(conn, now, account_ids)
    cash_updated = 0
    for row in cash:
        try:
            rate = price_api.current_fx_to_gbp(row['currency'])
        except Exception:
            rate = None
        value = _number((rate or {}).get('rate_to_gbp'))
        if value is None or value <= 0 or not _utc(rate.get('asof')):
            continue
        conn.execute('INSERT INTO fx_quotes(currency,rate_to_gbp,asof,fetched_at,source_url) VALUES(?,?,?,?,?) '
                     'ON CONFLICT(currency) DO UPDATE SET rate_to_gbp=excluded.rate_to_gbp,asof=excluded.asof,fetched_at=excluded.fetched_at,source_url=excluded.source_url',
                     (row['currency'], value, rate['asof'], now.isoformat(), rate.get('source_url')))
        cash_updated += 1
    conn.commit()
    return {'historical_attempted': len(needed), 'historical_updated': updated,
            'historical_failed': len(needed)-updated, 'cash_attempted': len(cash),
            'cash_updated': cash_updated, 'cash_failed': len(cash)-cash_updated}


def holdings_value(conn, now: datetime | None = None, account_ids=None) -> dict:
    """Known securities value from cached prices; completeness is explicit.

    market_value is the priced subtotal. Missing quotes never mean zero wealth.
    asof is the oldest included quote, so a fresh quote cannot hide stale ones.
    """
    result = _valuation_totals(_holding_positions(conn, account_ids), _cached_prices(conn),
                               _utc(now) or datetime.now(timezone.utc))
    _add_foreign_cash(conn, result, now, account_ids)
    return result


def household_holdings_value(conn, now: datetime | None = None, *, planning=False) -> dict:
    """Apply account inclusion and ownership only to household headlines.

    The source portfolio and its account selector continue to show full values.
    Without financially effective settings, preserve the original aggregation
    (including its rounding and number of distinct holdings) exactly.
    """
    from .account_settings import effective_settings
    accounts = [(row['id'], effective_settings(conn, row['id'])) for row in
                conn.execute("SELECT id FROM accounts WHERE account_type='investment' ORDER BY id")]
    def included_account(settings):
        return (settings['include_in_reports'] and not settings['archived']
                and (not planning or settings['include_in_planning']))

    if all(included_account(s) and s['ownership_share'] == 1
           for _, s in accounts):
        return holdings_value(conn, now)
    included = [(account, s['ownership_share']) for account, s in accounts
                if included_account(s)]
    now = _utc(now) or datetime.now(timezone.utc)
    if all(share == 1 for _, share in included):
        return holdings_value(conn, now, [account for account, _ in included])

    valued = [(holdings_value(conn, now, [account]), share) for account, share in included]
    result = holdings_value(conn, now, [])
    money_fields = (
        'market_value', 'cost_basis', 'known_cost_basis', 'priced_cost_basis',
        'unrealised', 'known_unrealised', 'realised', 'known_realised', 'total_pl',
        'estimated_cost_basis', 'known_estimated_cost_basis', 'estimated_unrealised',
        'known_estimated_unrealised', 'estimated_realised', 'known_estimated_realised',
        'estimated_total_pl', 'estimated_bought', 'estimated_sold',
    )
    for key in money_fields:
        result[key] = (round(sum(row[key] * share for row, share in valued), 2)
                       if all(row[key] is not None for row, _ in valued) else None)
    for key in ('n_priced', 'n_unpriced', 'n_stale', 'n_unknown_cost',
                'n_unknown_estimated_cost', 'n_unsupported_unrealised', 'n_estimated_cost'):
        result[key] = sum(row[key] for row, _ in valued)
    for key in ('position_complete', 'valuation_complete', 'valuation_fresh', 'cost_basis_complete'):
        result[key] = all(row[key] for row, _ in valued)
    for prefix in ('', 'estimated_'):
        value, cost = result[prefix + 'unrealised'], result[prefix + 'cost_basis']
        result[prefix + 'unrealised_pct'] = round(value / cost * 100, 1) if value is not None and cost else None
    for key, choose in (('asof', min), ('newest_asof', max)):
        stamps = [_utc(row[key]) for row, _ in valued if _utc(row[key]) is not None]
        result[key] = choose(stamps).isoformat() if stamps else None

    native = defaultdict(list)
    cash = defaultdict(list)
    for row, share in valued:
        for item in row['native_totals']:
            native[item['currency']].append((item, share))
        for item in row['foreign_cash']:
            cash[item['currency']].append((item, share))
    result['native_totals'] = []
    for currency, components in sorted(native.items()):
        item = {'currency': currency}
        for key in ('cost_basis', 'market_value', 'unrealised', 'realised'):
            item['known_' + key] = round(sum(row['known_' + key] * share for row, share in components), 2)
            item[key] = item['known_' + key] if all(row[key] is not None for row, _ in components) else None
        for key in ('n_holdings', 'n_unknown_cost', 'n_unpriced'):
            item[key] = sum(row[key] for row, _ in components)
        item['realised_complete'] = all(row['realised_complete'] for row, _ in components)
        result['native_totals'].append(item)
    result['foreign_cash'] = []
    for currency, components in sorted(cash.items()):
        amount = sum(row['amount'] * share for row, share in components)
        if abs(amount) < .005:
            continue
        # All accounts use the same cached quote for a currency. Revalue the
        # weighted native balance once, avoiding rounding each account's FX.
        item = dict(components[0][0], amount=round(amount, 2))
        item['value_gbp'] = (round(amount * item['rate_to_gbp'], 2)
                             if all(row['value_gbp'] is not None for row, _ in components) else None)
        result['foreign_cash'].append(item)
    result['foreign_cash_value'] = round(sum(row['value_gbp'] or 0 for row in result['foreign_cash']), 2)
    result['foreign_cash_unvalued'] = sum(row['value_gbp'] is None for row in result['foreign_cash'])
    result['foreign_cash_stale'] = sum(bool(row.get('stale')) for row in result['foreign_cash'])
    return result


def valuation(conn, refresh=False, now: datetime | None = None, account_ids=None) -> dict | None:
    """Per-holding valuation. Failed refreshes retain quotes and flag staleness."""
    if not conn.execute("SELECT 1 FROM accounts WHERE account_type='investment' LIMIT 1").fetchone():
        return None
    from .investment_accounts import available_accounts, selected_accounts
    account_ids = selected_accounts(conn, account_ids)
    from . import prices as price_api
    now = _utc(now) or datetime.now(timezone.utc)
    fx_refresh = _refresh_fx(conn, price_api, now, account_ids) if refresh else None
    positions = _holding_positions(conn, account_ids)
    held = [p for p in positions.values() if p["net_shares"] > 0]
    cached = _cached_prices(conn)
    todo = [{"isin": p["isin"], "symbol": (cached.get(p["isin"]) or {}).get("symbol"),
             "ticker": p["ticker"], "price_currency": p['execution_price_currency'] or p['native_currency']} for p in held
            if refresh or _price_refresh_due(cached.get(p["isin"]), now)]
    fetched = price_api.batch_quote(todo) if todo else {}
    updated_isins = set()
    for isin, q in fetched.items():
        if _usable_price(q) is None:
            continue
        values = {'isin': isin, 'symbol': q['symbol'], 'currency': q['currency'],
                  'price_native': q['price_native'], 'price_gbp': q['price_gbp'],
                  'updated': q.get('fetched_at') or now.isoformat()}
        available_columns = {row[1] for row in conn.execute('PRAGMA table_info(prices)')}
        for key in ('quote_asof', 'fetched_at', 'fx_rate_to_gbp', 'fx_asof', 'provider', 'source_url', 'identity_verified', 'quote_meta'):
            if key in available_columns:
                value = q.get(key)
                if key == 'quote_meta' and isinstance(value, (dict, list)):
                    value = json.dumps(value)
                values[key] = int(q.get(key, True)) if key == 'identity_verified' else value
        columns = list(values)
        conn.execute('INSERT INTO prices(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns)
                     + ') ON CONFLICT(isin) DO UPDATE SET ' + ','.join(f'{key}=excluded.{key}' for key in columns if key != 'isin'),
                     [values[key] for key in columns])
        updated_isins.add(isin)
    conn.commit()
    # A provider can fail without raising an API error. Report that outcome
    # separately from cache freshness, including when manual refreshes fail
    # while recent cached quotes are still considered fresh.
    failed_isins = [item['isin'] for item in todo if item['isin'] not in updated_isins]
    refresh_result = {
        "requested": bool(refresh), "attempted": len(todo),
        "updated": len(updated_isins), "failed": len(failed_isins),
        "retained": sum(_usable_price(cached.get(isin)) is not None for isin in failed_isins),
        "failed_isins": failed_isins,
        "attempted_at": now.isoformat() if todo else None,
        "status": ("not_needed" if not todo else "failed" if not updated_isins
                   else "partial" if failed_isins else "success"),
    }
    cached = _cached_prices(conn)
    rows = [_position_view(p, cached.get(p['isin']) or {}, now) for p in held]
    names = {a['id']: a['display_name'] for a in available_accounts(conn)}
    for row in rows:
        row['account_names'] = [names.get(a, a) for a in row['account_ids']]
    rows.sort(key=lambda r: (r["market_value"] is not None, r["market_value"] or 0), reverse=True)
    totals = _valuation_totals(positions, cached, now)
    _add_foreign_cash(conn, totals, now, account_ids)
    if fx_refresh is not None:
        refresh_result['fx'] = fx_refresh
    closed = [_position_view(p, cached.get(p['isin']) or {}, now) for p in positions.values() if p['net_shares'] <= 0]
    for row in closed:
        row['account_names'] = [names.get(a, a) for a in row['account_ids']]
    closed.sort(key=lambda row: (row['last_trade'] or '', row['ticker'] or ''), reverse=True)
    return {"holdings": rows, "closed_positions": closed, "totals": totals, "refresh": refresh_result,
            "selected_account_ids": account_ids}


# ----- persist computed intelligence ------------------------------------------

def recompute_all(conn, transfer_window=3, require_name=False, *, commit=True):
    """Refresh derived markers, optionally inside a caller-owned transaction."""
    cur = conn.cursor()
    cur.execute("DELETE FROM recurring")
    cur.execute("UPDATE transactions SET recurring_id=NULL, transfer_group=NULL, is_anomaly=0")

    for g in detect_transfers(conn, window_days=transfer_window, require_name=require_name):
        cur.execute("UPDATE transactions SET transfer_group=? WHERE id IN (?,?)",
                    (g["group"], g["out"]["id"], g["in"]["id"]))

    for r in detect_recurring(conn):
        cur.execute("INSERT INTO recurring(merchant,cadence_days,typical_amount,category,"
                    "first_seen,last_seen,n_occurrences,status,meta) VALUES(?,?,?,?,?,?,?,?,?)",
                    (r["merchant"], r["cadence_days"], r["typical_amount"], r["category"],
                     r["first_seen"], r["last_seen"], r["n_occurrences"], r["status"], r["cadence"]))
        rid = cur.lastrowid
        cur.executemany("UPDATE transactions SET recurring_id=? WHERE id=?",
                        [(rid, tid) for tid in r["txn_ids"]])

    for a in detect_anomalies(conn):
        cur.execute("UPDATE transactions SET is_anomaly=1 WHERE id=?", (a["id"],))

    result = {
        "recurring": cur.execute("SELECT COUNT(*) FROM recurring").fetchone()[0],
        "transfers_linked": cur.execute(
            "SELECT COUNT(*) FROM transactions WHERE transfer_group IS NOT NULL").fetchone()[0],
        "anomalies": cur.execute(
            "SELECT COUNT(*) FROM transactions WHERE is_anomaly=1").fetchone()[0],
    }
    if commit:
        conn.commit()
    return result

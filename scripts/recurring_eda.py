"""Reproduce aggregate recurring-payment EDA and retrospective rolling replay.

Run with an immutable ledger snapshot (opened read-only)::

    PYTHONPATH=backend python scripts/recurring_eda.py snapshot.db \
        --as-of 2026-09-18 --baseline path/to/analytics_before.py \
        --output output/private-recurring-eda/eda.json

Only aggregate counts and pseudonymous series IDs are written. Source amounts
remain private financial data: keep results in an ignored/private directory.
Replay trains on transactions and completed statements dated on/before each
origin. Current classifications are reused because historical classifications
are not available. Held-out transactions are only used for scoring. Matching
future merchant payments is a proxy, not manually verified subscription truth.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from app import analytics

BILL_CATEGORIES = {
    'Housing', 'Subscriptions', 'Phone & internet', 'Bills & utilities',
    'Health & education', 'Software & online services',
}


def module_from_path(path):
    spec = importlib.util.spec_from_file_location('app.recurring_eda_baseline', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _day(row):
    return date.fromisoformat(row['txn_date'][:10])


def normalized_label(label):
    return re.sub(r'[^a-z0-9]', '', (label or '').casefold())


def reviewed_merchant(row):
    try:
        fields = json.loads(row.get('classification') or '{}').get('reviewed_fields', [])
    except (TypeError, ValueError, AttributeError):
        fields = []
    return 'merchant' in fields


def logical_label(row):
    """Conservative evaluation identity, fixed independently of model outputs."""
    merchant = row.get('merchant') or ''
    if row.get('merchant_kind') == 'person' or reviewed_merchant(row):
        return merchant
    if row.get('category') == 'Subscriptions':
        match = re.fullmatch(r'amazon\s+prime\s+([a-z0-9]{5,14})(?:\s+amzn)?', merchant, re.I)
        if match and re.search(r'\d', match[1]) and re.search(r'[a-z]', match[1], re.I):
            return 'amazonprime'
    return normalized_label(merchant)


def is_bill(row):
    return row.get('category') in BILL_CATEGORIES or bool(re.search(
        r'\b(subs?|subscription|membership|premium|prime)\b', row.get('merchant') or '', re.I))


def series_id(key):
    return hashlib.sha256(repr(key).encode()).hexdigest()[:12]


def date_amounts(rows):
    grouped = defaultdict(float)
    for row in rows:
        grouped[_day(row)] += abs(row['amount'])
    return sorted(grouped.items())


def summarize_patterns(patterns):
    household = [p for p in patterns if 'pattern_type' in p]
    eligible = [p for p in household if p.get('forecast_eligible')]
    return {
        'detected_patterns': len(household),
        'pattern_types': dict(Counter(p['pattern_type'] for p in household)),
        'status_counts': dict(Counter(p['status'] for p in household)),
        'forecast_eligible': len(eligible),
        'forecast_monthly_cost': round(sum(p['monthly_cost'] for p in eligible), 2),
        'eligible_current_amounts_sum': round(sum(p['current_amount'] for p in eligible), 2),
        'eligible_cadences': dict(Counter(p['cadence'] for p in eligible)),
        'stale': sum(bool(p.get('data_stale')) for p in household),
    }


def metrics(records):
    observed = [r for r in records if r['outcome'] == 'observed_next_payment']
    mature = [r for r in records if r['outcome'] != 'right_censored']
    def mean(values):
        return round(statistics.mean(values), 4) if values else None
    return {
        'forecasts': len(records), 'mature_forecasts': len(mature),
        'observed_next_payments': len(observed),
        'no_payment_in_observed_window': sum(r['outcome'] == 'no_payment_in_observed_window' for r in records),
        'right_censored': sum(r['outcome'] == 'right_censored' for r in records),
        'date_mae_days': mean([r['date_absolute_error_days'] for r in observed]),
        'date_within_3_days_share': mean([r['date_absolute_error_days'] <= 3 for r in observed]),
        'date_within_7_days_share': mean([r['date_absolute_error_days'] <= 7 for r in observed]),
        'amount_mae_gbp': mean([r['amount_absolute_error'] for r in observed]),
        'amount_median_absolute_error_gbp': round(statistics.median(r['amount_absolute_error'] for r in observed), 4) if observed else None,
        'amount_within_10pct_or_1gbp_share': mean([r['amount_absolute_error'] <= max(1, r['actual_amount'] * .1) for r in observed]),
    }


def replay(conn, source_rows, spending_rows, models, today):
    """Forecast after each bill observation; score the next logical merchant day."""
    by_key = defaultdict(list)
    by_id = {r['id']: r for r in spending_rows}
    for row in spending_rows:
        if row.get('merchant'):
            by_key[(row['account_id'], logical_label(row))].append(row)
    observed_through = defaultdict(lambda: date.min)
    for row in source_rows:
        observed_through[row['account_id']] = max(observed_through[row['account_id']], _day(row))
    origins = sorted({_day(r) for r in spending_rows if is_bill(r)})
    accounts = [dict(r) for r in conn.execute('SELECT * FROM accounts')]
    statements = [dict(r) for r in conn.execute('SELECT * FROM statements WHERE period_end<=?', (today.isoformat(),))]
    context = sqlite3.connect(':memory:')
    context.row_factory = sqlite3.Row
    context.execute('CREATE TABLE accounts(id TEXT, account_type TEXT)')
    context.executemany('INSERT INTO accounts VALUES(?,?)', [(r['id'], r['account_type']) for r in accounts])
    context.execute('CREATE TABLE statements(account_id TEXT,period_end TEXT)')
    records = {name: [] for name in models}
    for origin in origins:
        train_source = [r for r in source_rows if _day(r) <= origin]
        train_spending = [r for r in spending_rows if _day(r) <= origin]
        context.execute('DELETE FROM statements')
        context.executemany('INSERT INTO statements VALUES(?,?)', [(s['account_id'], s['period_end']) for s in statements if s['period_end'][:10] <= origin.isoformat()])
        for name, model in models.items():
            patterns = model.detect_recurring(context, as_of=origin, source_rows=train_source, spending_rows=train_spending)
            for pattern in patterns:
                if not pattern.get('forecast_eligible') or pattern['last_seen'] != origin.isoformat() or not pattern.get('next_expected'):
                    continue
                # The original transaction membership defines the scoring key.
                train_members = [by_id[i] for i in pattern['txn_ids'] if i in by_id]
                if not train_members:
                    continue
                key = (pattern['account_id'], logical_label(train_members[-1]))
                predicted = date.fromisoformat(pattern['next_expected'])
                tolerance = max(7, round(pattern['cadence_days'] * .25))
                deadline = predicted + timedelta(days=tolerance)
                future = [(d, amount) for d, amount in date_amounts(by_key[key]) if origin < d <= deadline]
                rec = {'series': series_id(key), 'origin': origin.isoformat(), 'cadence': pattern['cadence'],
                       'predicted_date': predicted.isoformat(), 'predicted_amount': pattern['current_amount']}
                if future:
                    actual_date, actual_amount = future[0]
                    rec.update(outcome='observed_next_payment', actual_date=actual_date.isoformat(), actual_amount=round(actual_amount, 2),
                               date_absolute_error_days=abs((predicted-actual_date).days),
                               amount_absolute_error=round(abs(pattern['current_amount']-actual_amount), 4))
                else:
                    rec['outcome'] = 'no_payment_in_observed_window' if observed_through[pattern['account_id']] >= deadline else 'right_censored'
                records[name].append(rec)
    context.close()
    indexed = {name: {(r['series'], r['origin']): r for r in rs if r['outcome'] == 'observed_next_payment'} for name, rs in records.items()}
    common = set.intersection(*(set(x) for x in indexed.values())) if indexed else set()
    return {
        'method': 'Expanding history after each bill-like observation; forecast-eligible patterns whose last_seen equals origin. Dates and completed statements through origin only. Evaluate next conservatively normalized merchant-day within predicted date + max(7 days, 25% cadence). No later payment is not verified cancellation because account observations do not prove complete coverage.',
        'origin_dates': len(origins), 'origin_first': origins[0].isoformat() if origins else None,
        'origin_last': origins[-1].isoformat() if origins else None,
        'by_model': {name: metrics(rs) for name, rs in records.items()},
        'common_observed_origins': len(common),
        'common_cohort': {name: metrics([index[key] for key in sorted(common)]) for name, index in indexed.items()},
        'records': records,
    }


def explore(path, today, baseline_path=None, include_replay=True):
    path = Path(path).resolve()
    baseline = module_from_path(baseline_path) if baseline_path else analytics
    conn = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('BEGIN')
    all_rows = baseline._rows(conn)
    bank_rows = [r for r in all_rows if r['account_type'] != 'investment' and _day(r) <= today]
    spending = [component for r in bank_rows if (component := baseline._household_component(r)) is not None and component['amount'] < 0]
    exact, normalized, logical = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in spending:
        if row.get('merchant'):
            exact[(row['account_id'], row['merchant'])].append(row)
            normalized[(row['account_id'], normalized_label(row['merchant']))].append(row)
            logical[(row['account_id'], logical_label(row))].append(row)
    fragments = [rows for rows in normalized.values() if len({r['merchant'] for r in rows}) > 1]
    suffix_rows = [r for r in spending if logical_label(r) != normalized_label(r['merchant']) and not reviewed_merchant(r) and r.get('merchant_kind') != 'person']
    models = {'baseline': baseline, 'current': analytics} if baseline_path else {'current': analytics}
    patterns = {name: model.detect_recurring(conn, as_of=today, source_rows=bank_rows, spending_rows=spending) for name, model in models.items()}
    # Model-independent amount comparison, restricted to approximately monthly
    # held-out observations, with three or more preceding merchant-days.
    amount_holdouts = []
    for key, rows in logical.items():
        if not is_bill({'category': baseline._mode([r['category'] for r in rows]), 'merchant': rows[0]['merchant']}):
            continue
        events = date_amounts(rows)
        for i in range(3, len(events)):
            d, amount = events[i]
            if not 20 <= (d-events[i-1][0]).days <= 45:
                continue
            history = [v for _, v in events[:i]]
            amount_holdouts.append({'last_two_error': abs(statistics.median(history[-2:])-amount),
                                    'last_three_error': abs(statistics.median(history[-3:])-amount)})
    latest_changes = []
    same_day = []
    for key, rows in logical.items():
        if not any(is_bill(r) for r in rows):
            continue
        events = date_amounts(rows)
        if len(events) >= 5:
            old = statistics.median(v for _, v in events[:-2])
            a, b = events[-2][1], events[-1][1]
            current = statistics.median([a,b])
            if old and abs(a-b) <= max(.5, current*.05) and abs(current-old)/old >= .05:
                latest_changes.append({'series': series_id(key), 'category': baseline._mode([r['category'] for r in rows]),
                                       'earlier_median': old, 'recent_two_median': current,
                                       'change_percent': round((current/old-1)*100, 2), 'last_date': events[-1][0].isoformat()})
        counts = Counter(_day(r) for r in rows)
        if max(counts.values(), default=0) > 1:
            same_day.append({'series': series_id(key), 'transaction_count': len(rows),
                             'date_count': len(events), 'max_transactions_same_date': max(counts.values())})
    result = {
        'as_of': today.isoformat(), 'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'model_sources': {name: {'path': str(Path(model.__file__).resolve().relative_to(ROOT)) if Path(model.__file__).resolve().is_relative_to(ROOT) else Path(model.__file__).name,
                                 'sha256': hashlib.sha256(Path(model.__file__).read_bytes()).hexdigest()} for name, model in models.items()},
        'helper_sources': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in (ROOT / 'backend/app/recurring_model.py', ROOT / 'backend/app/recurring_identity.py') if p.exists()},
        'method': {'population': 'Posted ledger household debit components, excluding investment accounts and transactions after as_of.',
                   'grain': 'Account × conservatively normalized merchant × transaction date. Amounts are absolute debits in GBP ledger units.',
                   'classification_caveat': 'Current category/merchant/exclusion judgments are reused in historical replay; historical label revisions and statement ingestion times are unavailable.',
                   'coverage_caveat': 'One input ledger with unequal account histories and incomplete statement coverage; observed next payments are a proxy for prediction accuracy, not subscription ground truth.',
                   'privacy': 'Only aggregates and hashed series references are exported; no raw merchant labels, account identifiers, descriptions or transaction IDs.'},
        'scope': {'ledger_rows': len(all_rows), 'eligible_bank_rows': len(bank_rows), 'household_debit_rows': len(spending),
                  'bank_accounts': len({r['account_id'] for r in bank_rows}), 'date_first': min((_day(r).isoformat() for r in bank_rows), default=None),
                  'date_last': max((_day(r).isoformat() for r in bank_rows), default=None), 'exact_merchant_account_groups': len(exact)},
        'quality': {'integrity': conn.execute('PRAGMA integrity_check').fetchone()[0],
                    'foreign_key_errors': len(conn.execute('PRAGMA foreign_key_check').fetchall()),
                    'future_bank_transactions_excluded': sum(r['account_type'] != 'investment' and _day(r)>today for r in all_rows),
                    'missing_merchant_debits': sum(not r.get('merchant') for r in spending),
                    'invalid_or_missing_dates': conn.execute('SELECT count(*) FROM transactions').fetchone()[0]-len(all_rows)},
        'fragmentation': {'case_punctuation_groups': len(fragments), 'case_punctuation_transactions': sum(map(len, fragments)),
                          'bill_case_punctuation_groups': sum(any(is_bill(r) for r in rows) for rows in fragments),
                          'bill_case_punctuation_transactions': sum(len(rows) for rows in fragments if any(is_bill(r) for r in rows)),
                          'conservative_suffix_transactions': len(suffix_rows), 'conservative_suffix_labels': len({r['merchant'] for r in suffix_rows}),
                          'conservative_suffix_account_groups': len({r['account_id'] for r in suffix_rows}),
                          'logical_merchant_account_groups': len(logical)},
        'patterns': {name: summarize_patterns(ps) for name, ps in patterns.items()},
        'recent_stable_amount_shifts': latest_changes,
        'same_merchant_same_day_multiplicity': same_day,
        'amount_holdout': {'count': len(amount_holdouts),
                           'last_two_mae_gbp': round(statistics.mean(r['last_two_error'] for r in amount_holdouts), 4) if amount_holdouts else None,
                           'last_three_mae_gbp': round(statistics.mean(r['last_three_error'] for r in amount_holdouts), 4) if amount_holdouts else None,
                           'last_three_better': sum(r['last_three_error']<r['last_two_error'] for r in amount_holdouts),
                           'last_three_worse': sum(r['last_three_error']>r['last_two_error'] for r in amount_holdouts),
                           'ties': sum(r['last_three_error']==r['last_two_error'] for r in amount_holdouts),
                           'scope': 'All bill-like logical merchant series; next observed gaps 20–45 days and at least three prior observations. Extras and payment changes remain in sample.'},
    }
    timings = {name: [] for name in models}
    # Interleave models and reverse their order on alternate repetitions to
    # reduce sensitivity to changing load from other local work.
    for repeat in range(9):
        order = list(models.items())
        if repeat % 2:
            order.reverse()
        for name, model in order:
            start = time.perf_counter()
            model.detect_recurring(conn, as_of=today)
            timings[name].append((time.perf_counter()-start)*1000)
    result['performance'] = {
        name: {'repeats': len(elapsed), 'median_ms': round(statistics.median(elapsed), 3),
               'min_ms': round(min(elapsed), 3), 'max_ms': round(max(elapsed), 3)}
        for name, elapsed in timings.items()
    }
    result['performance']['method'] = 'Nine interleaved detector calls per model on the same read-only snapshot including row loading; alternating model order, warm interpreter, no API/network overhead. Wall-clock values may reflect other local processes.'
    if include_replay:
        result['replay'] = replay(conn, bank_rows, spending, models, today)
    conn.rollback()
    conn.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database')
    parser.add_argument('--as-of', type=date.fromisoformat, default=date.today())
    parser.add_argument('--baseline', help='Saved app/analytics.py from before the change')
    parser.add_argument('--output', required=True)
    parser.add_argument('--skip-replay', action='store_true')
    args = parser.parse_args()
    result = explore(args.database, args.as_of, args.baseline, not args.skip_replay)
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2)+'\n')
    target.chmod(0o600)
    compact = {key: result[key] for key in ('scope', 'quality', 'fragmentation', 'patterns', 'amount_holdout', 'performance')}
    if 'replay' in result:
        compact['replay'] = {k:v for k,v in result['replay'].items() if k != 'records'}
    print(json.dumps(compact, indent=2))


if __name__ == '__main__':
    main()

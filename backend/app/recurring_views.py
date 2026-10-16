"""Inspectable recurring patterns and subscription labels awaiting timing evidence."""
import hashlib
import json
from datetime import date

from . import analytics
from .recurring_identity import grouped_payments
from .workspace import semantic


def series_key(account_id, merchant, identity=None):
    # Independent of the disposable IDs in the recomputed recurring table.
    return hashlib.sha256(json.dumps([account_id, identity or merchant], ensure_ascii=False).encode()).hexdigest()[:24]


def overview(conn, patterns, *, spending_rows=None, as_of=None):
    accounts = {r['id']: r['display_name'] for r in conn.execute('SELECT id,display_name FROM accounts')}
    transactions = {r['id']: dict(r) for r in conn.execute(
        'SELECT id,account_id,txn_date,description,source_merchant,merchant,amount,category FROM transactions')}
    spending = spending_rows if spending_rows is not None else analytics._spending_rows(conn)
    components = {row['id']: row for row in spending if row.get('analysis_component')}

    def payment(ident):
        row = transactions[ident]
        result = {**row, 'account': accounts.get(row['account_id'], row['account_id'])}
        if component := components.get(ident):
            # Keep the original conversion amount inspectable while explaining
            # that only its printed fee establishes this spending pattern.
            result.update(analysis_amount=component['amount'], analysis_category=component['category'],
                          analysis_component=component['analysis_component'])
        return result

    def decorate(row):
        return {**row, 'key': series_key(row['account_id'], row['merchant'], row.get('series_identity')),
                'account': accounts.get(row['account_id'], row['account_id']),
                'payments': sorted((payment(i) for i in row['txn_ids']),
                                   key=lambda p: (p['txn_date'], p['id']), reverse=True)}

    detected = {ident for row in patterns for ident in row['txn_ids']}
    today = as_of or date.today()
    pending = [row for row in spending if semantic(row) == 'Subscriptions' and row['id'] not in detected
               and analytics._d(row['txn_date']) and analytics._d(row['txn_date']) <= today]
    candidates = []
    for account_id, merchant, identity, payments in grouped_payments(pending):
        dates = sorted({r['txn_date'] for r in payments})
        candidates.append(decorate({
            'account_id': account_id, 'merchant': merchant, 'category': payments[0]['category'],
            'series_identity': json.dumps(identity), 'merchant_labels': sorted({r['merchant'] for r in payments}),
            'first_seen': dates[0], 'last_seen': dates[-1], 'n_occurrences': len(dates),
            'txn_ids': [r['id'] for r in payments],
            'reason': 'Classified as a subscription, but the observed dates do not establish a pattern at these settings.',
            'pattern_type':'unconfirmed','forecast_eligible':False,
            'forecast_reason':'Not enough consistent timing evidence to forecast a payment.',
            'evidence_count':len(dates),
        }))
    return {'patterns': [decorate(r) for r in patterns],
            'candidates': sorted(candidates, key=lambda r: (r['merchant'].casefold(), r['account_id']))}

"""Read-only bank evidence and activity behind an explicitly saved identity.

Provider text is evidence, not a verified business/person identity. These reads
never classify transactions, contact a provider, or include raw response blobs.
"""
from collections import defaultdict

from .lunchflow_store import has_cache
from .lunchflow_client import merchant_label


def transaction_evidence(conn, transaction_id):
    if not has_cache(conn):
        return None
    row = conn.execute("""SELECT b.merchant,b.description,b.txn_date AS date,
        b.amount*c.amount_multiplier AS amount,b.currency,b.status,
        b.last_seen_at AS fetched_at,c.institution_name AS bank
        FROM bank_transactions b JOIN bank_connections c ON c.id=b.connection_id
        WHERE b.transaction_id=? AND b.is_pending=0
        AND b.status IN ('matched','imported')""", (transaction_id,)).fetchone()
    if not row:
        return None
    evidence = dict(row)
    # Some banks put a payment reference in the merchant field. Preserve the raw
    # cache, but never present that reference as a business or person's name.
    evidence.update(merchant_label(evidence.get('merchant'), evidence.get('description')))
    return {**evidence, 'note': 'Bank-supplied text linked to this ledger entry. Your saved identity and category remain authoritative; the bank may use a different posting date.'}


def merchant_activity(conn, merchant_id):
    merchant = conn.execute('SELECT id,canonical_name,kind,category,spending_treatment,source,source_url FROM merchants WHERE id=? AND enabled=1', (merchant_id,)).fetchone()
    if not merchant:
        raise LookupError('This saved name is no longer available.')
    # Identity comes only from the saved classifier. Similar text is never a join.
    rows = [dict(row) for row in conn.execute("""SELECT t.id,t.txn_date,t.description,
        t.merchant,t.category,t.amount,t.account_id,a.display_name AS account,
        t.spending_treatment,t.exclude_from_analysis,t.transfer_group
        FROM transactions t JOIN accounts a ON a.id=t.account_id
        WHERE json_extract(t.classification,'$.merchant_id')=?
        AND a.account_type!='investment' AND COALESCE(t.currency,'GBP')='GBP' ORDER BY t.txn_date DESC,t.id DESC""", (merchant_id,))]
    monthly = defaultdict(lambda: {'money_in': 0, 'money_out': 0, 'payments': 0})
    accounts = {}
    money_in = money_out = 0
    for row in rows:
        amount = row['amount']
        incoming, outgoing = max(amount, 0), max(-amount, 0)
        money_in += incoming
        money_out += outgoing
        accounts[row['account_id']] = row['account']
        if row['txn_date']:
            month = monthly[row['txn_date'][:7]]
            month['money_in'] += incoming
            month['money_out'] += outgoing
            month['payments'] += 1
    bank_labels = []
    if has_cache(conn):
        labels = {}
        for row in conn.execute("""SELECT b.merchant,b.description,
            c.institution_name AS bank,COUNT(*) AS transactions,MAX(b.last_seen_at) AS fetched_at
            FROM bank_transactions b JOIN transactions t ON t.id=b.transaction_id
            JOIN accounts a ON a.id=t.account_id
            JOIN bank_connections c ON c.id=b.connection_id
            WHERE json_extract(t.classification,'$.merchant_id')=? AND a.account_type!='investment'
            AND b.is_pending=0 AND b.status IN ('matched','imported') AND b.merchant!=''
            GROUP BY b.merchant,b.description,c.institution_name""", (merchant_id,)):
            label = merchant_label(row['merchant'], row['description'])['merchant']
            if label:
                item = labels.setdefault((label, row['bank']), {'label': label, 'bank': row['bank'], 'transactions': 0, 'fetched_at': row['fetched_at']})
                item['transactions'] += row['transactions']
                item['fetched_at'] = max(item['fetched_at'], row['fetched_at'])
        bank_labels = sorted(labels.values(), key=lambda item: (-item['transactions'], item['label']))[:20]
    fields = ('id', 'txn_date', 'description', 'merchant', 'category', 'amount', 'account_id', 'account')
    dates = [row['txn_date'] for row in rows if row['txn_date']]
    return {'merchant': dict(merchant), 'currency': 'GBP',
            'summary': {'transactions': len(rows), 'money_in': round(money_in, 2),
                        'money_out': round(money_out, 2), 'net': round(money_in-money_out, 2),
                        'first_seen': min(dates) if dates else None, 'last_seen': max(dates) if dates else None,
                        'active_months': len(monthly), 'accounts': len(accounts)},
            'accounts': [{'id': key, 'name': value} for key, value in accounts.items()],
            'monthly': [{'month': month, **{k: round(v, 2) for k, v in values.items()}}
                        for month, values in sorted(monthly.items())],
            'transactions': [{key: row[key] for key in fields} for row in rows[:50]],
            'bank_labels': bank_labels,
            'method': 'Recorded GBP ledger movement for this saved identity, across available history. Includes transfers and refunds; money out is not net household spending. Pending, unresolved and native foreign-currency feed entries are excluded. Bank labels are observed text, not independently verified identities.'}

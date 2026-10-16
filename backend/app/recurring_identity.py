"""Conservative display-label grouping for derived recurring evidence only.

Never rewrites transactions or joins accounts. No fuzzy merchant matching: the
only reference suffix handled here is the observed Amazon Prime statement form.
User-reviewed names and person names remain exact.
"""
import json
import re
from collections import Counter, defaultdict
from .workspace import semantic


def identity(row):
    merchant = row.get('merchant') or ''
    if row.get('account_type') == 'investment' or row.get('merchant_kind') == 'person':
        return ('exact', merchant)
    try:
        reviewed = json.loads(row.get('classification') or '{}').get('reviewed_fields', [])
    except (TypeError, ValueError, AttributeError):
        reviewed = []
    if 'merchant' in reviewed:
        return ('exact', merchant)
    normalized = ' '.join(re.findall(r'\w+', merchant.casefold()))
    if semantic(row) == 'Subscriptions':
        match = re.fullmatch(r'amazon prime ([a-z0-9]{5,14})(?: amzn)?', normalized)
        if match and re.search(r'[a-z]', match[1]) and re.search(r'\d', match[1]):
            normalized = 'amazon prime'
    return ('normalized', normalized or merchant)


def grouped_payments(rows):
    rows = list(rows)
    protected = {(row['account_id'], row.get('merchant')) for row in rows
                 if identity(row)[0] == 'exact'}
    groups = defaultdict(list)
    for row in rows:
        if row.get('merchant'):
            key = ('exact', row['merchant']) if (row['account_id'], row['merchant']) in protected else identity(row)
            groups[(row['account_id'], key)].append(row)
    for (account_id, key), payments in groups.items():
        labels = Counter(row['merchant'] for row in payments)
        # Prefer the established spelling; tie-breaking never depends on row order.
        merchant = min(labels, key=lambda label: (-labels[label], label.casefold(), label))
        if key == ('normalized', 'amazon prime'):
            merchant = 'Amazon Prime'
        yield account_id, merchant, key, payments

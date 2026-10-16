"""Read-only checks of credit-balance evidence, never replacement valuations.

A provider's amount/sign alone does not establish whether it represents debt or
available credit. Statement roll-forwards and possible payments are diagnostics:
incomplete transaction coverage cannot prove a card's present balance.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import json
import math
import re


def _day(value):
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def _number(value):
    try:
        result = float(value) if value is not None else None
        return result if result is not None and math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _metadata(value):
    try:
        result = json.loads(value or '{}')
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def _posting_day(row):
    meta = _metadata(row['meta'])
    return _day(meta.get('posting_date') or meta.get('posted_date') or row['txn_date'])


def _potential_repayments(conn, account_id, bank, statement_date, statement_balance, today):
    """Only identify payment evidence; never infer a destination posting."""
    bank_key = re.sub(r'[^a-z0-9]', '', (bank or '').lower())
    if not bank_key:
        return []
    result = []
    for row in conn.execute('''SELECT t.txn_date,t.amount,t.description,t.merchant,t.meta
            FROM transactions t JOIN accounts a ON a.id=t.account_id
            WHERE t.account_id<>? AND a.account_type NOT IN ('investment','credit')
            AND t.amount<0 ORDER BY t.txn_date,t.id''', (account_id,)):
        posted = _posting_day(row)
        if not posted or not statement_date < posted <= today:
            continue
        label = ' '.join(str(row[key] or '') for key in ('description', 'merchant'))
        normalized = re.sub(r'[^a-z0-9]', '', label.lower())
        amex = bank_key in {'amex', 'americanexpress'}
        matches_bank = (any(key in normalized for key in ('amex', 'americanexp'))
                        if amex else bank_key in normalized)
        amount = round(-row['amount'], 2)
        matches_balance = statement_balance < 0 and abs(amount + statement_balance) < .005
        card_label = bool(re.search(r'\b(credit|card|rewards)\b', label, re.I))
        if matches_bank and (amex or card_label or matches_balance):
            result.append({'date': posted, 'amount': amount,
                           'matches_statement_balance': matches_balance})
    return result


def annotate_credit_balance_checks(conn, accounts, *, today=None):
    """Attach ``balance_check`` to wealth accounts with ``kind == 'credit'``.

    Call after wealth assigns native_value, currency and as_of. All amounts are
    native signed account values except payment/pending magnitudes. The returned
    diagnostic does not change observations, selection or reported valuations.
    Minimal/older databases without transaction or bank-cache tables are valid.
    """
    today = (today or datetime.now(timezone.utc).date()).isoformat()
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    has_transactions = 'transactions' in tables
    has_bank_transactions = {'bank_connections', 'bank_transactions'} <= tables
    for account in accounts:
        if account.get('kind') != 'credit':
            continue
        account_id = (account.get('drilldown') or {}).get('account_id')
        if not account_id and str(account.get('id', '')).startswith('statement:'):
            account_id = account['id'].split(':', 1)[1]
        reported = _number(account.get('native_value'))
        connected = account.get('source') == 'lunchflow'
        check = {'status': 'unverified', 'requires_review': True,
                 'statement_date': None, 'statement_balance': None,
                 'transaction_estimate': None, 'reported_balance': reported,
                 'reported_as_of': account.get('as_of'), 'currency': account.get('currency') or 'GBP',
                 'difference': None, 'pending_amount': 0.0, 'pending_count': 0,
                 'review_count': 0, 'potential_repayments': [], 'bank_as_of': None,
                 'message': 'The current card balance cannot be checked against a reconciled statement.'}
        account['balance_check'] = check
        if not account_id or 'statements' not in tables:
            continue
        statements = list(conn.execute('''SELECT id,period_end,closing_balance,reconciled
            FROM statements WHERE account_id=? AND period_end<=?
            AND closing_balance IS NOT NULL ORDER BY period_end DESC,id DESC''', (account_id, today)))
        if not statements:
            continue
        latest = statements[0]
        when = _day(latest['period_end'])
        closing = _number(latest['closing_balance'])
        if not when or closing is None:
            continue
        check.update(statement_date=when, statement_balance=round(-closing, 2))
        peers = [row for row in statements if _day(row['period_end']) == when]
        reliable = all(row['reconciled'] == 1 and _number(row['closing_balance']) == closing for row in peers)
        # Current importers express card statements in GBP. A different feed
        # currency must not be compared numerically with a GBP statement.
        same_currency = check['currency'] == 'GBP'
        if reliable and same_currency and has_transactions:
            change = 0.0
            for row in conn.execute('SELECT txn_date,amount,meta FROM transactions WHERE account_id=?', (account_id,)):
                posted = _posting_day(row)
                if posted and when < posted <= today:
                    change += row['amount']
            check['transaction_estimate'] = round(-closing + change, 2)
        if has_bank_transactions:
            for row in conn.execute('''SELECT t.txn_date,t.amount,t.is_pending,t.status,c.amount_multiplier
                    FROM bank_transactions t JOIN bank_connections c ON c.id=t.connection_id
                    WHERE c.account_id=? AND c.excluded=0 AND t.txn_date>? AND t.txn_date<=?
                    AND (t.is_pending=1 OR t.status='review')''', (account_id, when, today)):
                if row['is_pending'] and row['status'] == 'pending':
                    check['pending_count'] += 1
                    check['pending_amount'] += abs(row['amount'] * row['amount_multiplier'])
                if row['status'] == 'review':
                    check['review_count'] += 1
            check['pending_amount'] = round(check['pending_amount'], 2)
        if has_transactions and same_currency:
            check['potential_repayments'] = _potential_repayments(
                conn, account_id, account.get('institution'), when, -closing, today)
        if not connected:
            old = when < today
            check.update(status='statement_only', requires_review=old or not reliable,
                         message=('This is a statement balance, not a confirmed current card balance. '
                                  'Later spending and repayments may have changed it.' if old else
                                  'This is the reported statement balance; subsequent card activity is not confirmed.'))
            if check['potential_repayments']:
                check['requires_review'] = True
                check['message'] += (' Later payments to this card provider appear in another account; '
                                     'they have not been subtracted without a matching card balance.')
        elif check['transaction_estimate'] is not None and reported is not None:
            difference = round(reported - check['transaction_estimate'], 2)
            material = abs(difference) > max(1.0, check['pending_amount'] + 1.0)
            check.update(difference=difference,
                         status='discrepancy' if material else 'no_discrepancy',
                         requires_review=material or bool(check['review_count']))
            check['message'] = (
                'The connected balance differs materially from the last reconciled statement plus saved card activity. '
                'Check whether the provider is sending the amount owed or available credit; its balance type and bank timestamp are not supplied.'
                if material else
                'The connected balance is close to the statement plus saved card activity. '
                'This comparison does not prove complete coverage or a current bank balance.')
        if check['transaction_estimate'] is not None:
            check['message'] += ' The transaction estimate is diagnostic, not a confirmed balance.'
        if check['review_count']:
            check['message'] += ' Some later card transactions still await overlap review.'
        if check['pending_count']:
            check['message'] += ' Pending card activity is excluded from the transaction estimate.'

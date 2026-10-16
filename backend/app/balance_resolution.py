"""Resolve current credit valuations without changing any source observations.

A conflicting provider value is not suitable for a current subtotal. A recent,
successfully synced and fully linked transaction cache can support an explicitly
estimated statement roll-forward. It still cannot prove the bank's live debt.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from .bank_balance_rules import available_credit_rule
from .wealth_checks import annotate_credit_balance_checks, _day, _number, _posting_day


def _coverage(conn, account_id, statement_day, today, tables):
    """Evidence for using a roll-forward, not a claim of provider completeness."""
    evidence = {'eligible': False, 'synced_as_of': None, 'first_cached_date': None,
                'booked_count': 0, 'unresolved_count': 0, 'reason': None}
    if (today - date.fromisoformat(statement_day)).days > 62:
        evidence['reason'] = 'The reconciled statement is more than two statement cycles old.'
        return evidence
    required = {'bank_connections', 'bank_transactions', 'transactions'}
    if not required <= tables:
        evidence['reason'] = 'A connected transaction history is unavailable.'
        return evidence
    connections = list(conn.execute('''SELECT id,currency,status,excluded,present,
            amount_multiplier,last_synced_at,last_error FROM bank_connections
            WHERE account_id=? AND excluded=0''', (account_id,)))
    if len(connections) != 1:
        evidence['reason'] = 'A single mapped transaction connection is required.'
        return evidence
    connection = connections[0]
    synced_day = _day(connection['last_synced_at'])
    evidence['synced_as_of'] = connection['last_synced_at']
    if (connection['status'] != 'ACTIVE' or not connection['present']
            or connection['last_error'] or connection['currency'] != 'GBP'
            or synced_day is None or not statement_day <= synced_day <= today.isoformat()
            or (today - date.fromisoformat(synced_day)).days > 2):
        evidence['reason'] = 'The card needs a recent successful balance and transaction refresh.'
        return evidence

    rows = list(conn.execute('''SELECT b.txn_date,b.amount,b.currency,b.is_pending,b.status,
            b.transaction_id,t.account_id AS ledger_account_id,t.txn_date AS ledger_date,
            t.amount AS ledger_amount,t.meta AS ledger_meta
            FROM bank_transactions b LEFT JOIN transactions t ON t.id=b.transaction_id
            WHERE b.connection_id=? ORDER BY b.txn_date,b.id''', (connection['id'],)))
    booked_days = [_day(row['txn_date']) for row in rows if not row['is_pending']]
    booked_days = [day for day in booked_days if day and day <= synced_day]
    evidence['first_cached_date'] = min(booked_days, default=None)
    if not booked_days or min(booked_days) > statement_day:
        evidence['reason'] = 'The cached card activity does not reach the statement boundary.'
        return evidence
    for row in rows:
        posted = (_posting_day({'txn_date': row['ledger_date'], 'meta': row['ledger_meta']})
                  if row['ledger_date'] else _day(row['txn_date']))
        if row['is_pending'] or not posted or not statement_day < posted <= today.isoformat():
            continue
        # An explicit ignore decision preserves the already accepted ledger;
        # an ignored duplicate without a ledger link adds no new activity.
        if row['status'] == 'ignored':
            continue
        evidence['booked_count'] += 1
        amount = _number(row['amount'])
        linked_amount = _number(row['ledger_amount'])
        if (row['status'] not in {'matched', 'imported'} or not row['transaction_id']
                or row['ledger_account_id'] != account_id or row['currency'] != 'GBP'
                or amount is None or linked_amount is None
                or abs(amount * connection['amount_multiplier'] - linked_amount) >= .005
                or posted > synced_day):
            evidence['unresolved_count'] += 1
    if evidence['unresolved_count']:
        evidence['reason'] = 'Later booked card activity still needs reconciliation.'
        return evidence
    # The estimate must end at the successfully refreshed cache, including when
    # a manually imported transaction uses a posting date after its txn_date.
    for row in conn.execute('SELECT txn_date,meta FROM transactions WHERE account_id=?', (account_id,)):
        posted = _posting_day(row)
        if posted and synced_day < posted <= today.isoformat():
            evidence['reason'] = 'Recorded activity extends beyond the last successful card refresh.'
            return evidence
    evidence['eligible'] = True
    return evidence


def resolve_credit_balance(conn, account_id, *, reported_balance, source,
                           currency='GBP', reported_as_of=None, institution=None,
                           today=None):
    """Return the effective *current* value plus preserved reported evidence.

    All values use the ledger sign convention: debt negative, overpayment
    positive. Unknown values are None, never zero. Callers must not use this
    projection to rewrite historical observations or provider cache entries.
    ``today`` is an optional date for deterministic reads/tests.
    """
    today = today or datetime.now(timezone.utc).date()
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if institution is None and 'accounts' in tables:
        account = conn.execute('SELECT bank FROM accounts WHERE id=?', (account_id,)).fetchone()
        institution = account['bank'] if account else None
    reported = _number(reported_balance)
    account = {'id': 'statement:' + str(account_id), 'kind': 'credit',
               'drilldown': {'account_id': account_id}, 'institution': institution,
               'source': source, 'currency': currency, 'native_value': reported,
               'as_of': reported_as_of}
    annotate_credit_balance_checks(conn, [account], today=today)
    check = account['balance_check']
    result = {'effective_value': reported, 'effective_as_of': _day(reported_as_of),
              'reported_balance': reported, 'reported_as_of': reported_as_of,
              'reported_source': source,
              'basis': 'reported', 'estimated': False, 'current_unknown': reported is None,
              'pending_amount': check['pending_amount'], 'balance_check': check,
              'explanation': 'The latest reported card balance is included.',
              'coverage': None}

    def unknown(explanation):
        result.update(effective_value=None, effective_as_of=None, basis='unknown',
                      current_unknown=True, explanation=explanation)
        check.update(requires_review=True, message=explanation)
        return result

    if source == 'manual':
        if reported is None:
            return unknown('No manual card balance has been recorded for this date.')
        explanation = ('The manually recorded card balance is included for its observation date. '
                       'Later spending and repayments are not inferred from older statements.')
        result['explanation'] = explanation
        check.update(status='unverified', requires_review=False, message=explanation)
        return result

    if source != 'lunchflow':
        if check['potential_repayments']:
            return unknown('A payment to this card provider appears after the saved statement. '
                           'The statement remains in history; the current card balance is unknown '
                           'and is excluded from current totals until a newer balance is available.')
        result['explanation'] = 'The latest statement balance is included; later card activity is not confirmed.'
        if reported is None:
            return unknown('No supported card balance is available for the current total.')
        return result

    rule = available_credit_rule(conn, account_id, currency)
    if rule and reported is not None:
        # Undo the mapping's sign exactly; abs() would misread over-limit
        # available credit and manufacture the wrong outstanding balance.
        available = Decimal(str(reported)) / Decimal(rule['balance_multiplier'])
        from .currency_reporting import currency_quantum
        effective = float((available - Decimal(str(rule['credit_limit']))).quantize(currency_quantum(currency)))
        explanation = ('The provider reports available credit. The current card balance is available credit '
                       'minus the confirmed credit limit. Pending activity is not added separately.')
        result.update(effective_value=effective, basis='available_credit', estimated=False,
                      current_unknown=False, available_credit=float(available),
                      credit_limit=rule['credit_limit'], credit_limit_confirmed_at=rule['confirmed_at'],
                      explanation=explanation)
        check.update(diagnostic_status=check['status'], provider_difference=check['difference'],
                     status='available_credit', requires_review=False, message=explanation,
                     available_credit=float(available), credit_limit=rule['credit_limit'],
                     effective_balance=effective,
                     difference=(round(effective - check['transaction_estimate'], 2)
                                 if check['transaction_estimate'] is not None else None))
        return result

    if check['status'] != 'discrepancy':
        if reported is None:
            return unknown('No supported card balance is available for the current total.')
        return result

    coverage = _coverage(conn, account_id, check['statement_date'], today, tables)
    result['coverage'] = coverage
    if check['review_count'] or not coverage['eligible']:
        reason = coverage['reason'] or 'Later card transactions still await overlap review.'
        return unknown('The connected card balance conflicts with the reconciled statement and recorded activity. '
                       + reason + ' The current balance is unknown and excluded from current totals.')
    estimate = check['transaction_estimate']
    # Both values are guaranteed by the discrepancy check, but keep this
    # boundary explicit if the evidence checker is extended in future.
    if estimate is None:
        return unknown('The connected card balance conflicts with the available evidence; '
                       'no supported current estimate is available.')
    explanation = ('An estimated card balance is used: the last reconciled statement plus recorded booked '
                   'card activity. The conflicting provider balance is retained separately. '
                   'This is not a confirmed live balance; pending activity is excluded.')
    result.update(effective_value=estimate, effective_as_of=_day(coverage['synced_as_of']),
                  basis='statement_activity_estimate', estimated=True, current_unknown=False,
                  explanation=explanation)
    check.update(requires_review=True, message=explanation)
    return result

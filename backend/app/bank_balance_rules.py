"""Explicit, reversible interpretations of a connected card balance.

Preferences are bound to one provider connection, local account and currency.
They never replace provider observations or infer a credit limit from activity.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import re


PREFIX = 'bank_balance_rule:'


def _limit(value, currency, *, legacy=False):
    from .currency_reporting import currency_precision, currency_quantum
    precision = max(2, currency_precision(currency)) if legacy else currency_precision(currency)
    quantum = Decimal(1).scaleb(-precision) if legacy else currency_quantum(currency)
    message = ('Enter a positive credit limit in whole units.' if precision == 0 else
               f'Enter a positive credit limit with at most {precision} decimal places.')
    if isinstance(value, bool):
        raise ValueError(message)
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount != amount.quantize(quantum):
            raise ValueError
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(message) from None
    return float(amount)


def _connection(conn, provider_id):
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'bank_connections', 'accounts'} <= tables:
        return None
    return conn.execute('''SELECT c.*,a.account_type FROM bank_connections c
        LEFT JOIN accounts a ON a.id=c.account_id WHERE c.id=?''', (provider_id,)).fetchone()


def get_rule(conn, provider_id):
    """Return a valid, still-bound saved rule; reads never migrate or write."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='preferences'").fetchone():
        return None
    saved = conn.execute('SELECT value FROM preferences WHERE key=?', (PREFIX + str(provider_id),)).fetchone()
    if not saved:
        return None
    try:
        rule = json.loads(saved['value'])
        if not isinstance(rule, dict) or rule.get('balance_type') not in {'available_credit', 'reported'}:
            return None
        stamp = datetime.fromisoformat(rule['confirmed_at'])
        if stamp.tzinfo is None:
            return None
        connection = _connection(conn, provider_id)
        if (connection is None or rule.get('provider_id') != str(provider_id)
                or rule.get('account_id') != connection['account_id']
                or rule.get('currency') != connection['currency']
                or rule.get('connection_first_seen_at') != connection['first_seen_at']):
            return None
        if rule['balance_type'] == 'available_credit':
            if (connection['account_type'] != 'credit' or connection['excluded']
                    or not re.fullmatch('[A-Z]{3}', rule['currency'] or '')):
                return None
            # Previously confirmed zero-decimal-currency limits could contain
            # two decimal places. Retain their available-credit interpretation;
            # rejecting the rule would count raw available credit as an asset.
            # New confirmations use the currency quantum, and saved historical
            # valuations remain immutable.
            rule['credit_limit'] = _limit(rule.get('credit_limit'), rule['currency'], legacy=True)
            from .currency_reporting import currency_quantum
            limit = Decimal(str(rule['credit_limit']))
            if limit != limit.quantize(currency_quantum(rule['currency'])):
                rule['legacy_fractional_limit'] = True
        return rule
    except (ValueError, TypeError, KeyError):
        return None


def _record_rule_event(conn, rule):
    if rule.get('currency') is None or rule.get('account_id') is None:
        return  # An untyped feed has no dated card valuation to preserve.
    conn.execute('''INSERT OR IGNORE INTO bank_credit_rule_events
        (connection_id,account_id,currency,connection_first_seen_at,balance_type,credit_limit,confirmed_at)
        VALUES(?,?,?,?,?,?,?)''',
        (rule['provider_id'],rule['account_id'],rule['currency'],rule['connection_first_seen_at'],
         rule['balance_type'],rule.get('credit_limit'),rule['confirmed_at']))


def rule_history(conn, provider_id):
    """Return only rule events still bound to this exact provider account."""
    connection = _connection(conn, provider_id)
    if connection is None:
        return []
    rows = []
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bank_credit_rule_events'").fetchone():
        rows = [dict(row) for row in conn.execute('''SELECT balance_type,credit_limit,confirmed_at
            FROM bank_credit_rule_events WHERE connection_id=? AND account_id=?
              AND currency=? AND connection_first_seen_at=? ORDER BY confirmed_at,id''',
            (provider_id,connection['account_id'],connection['currency'],connection['first_seen_at']))]
    current = get_rule(conn, provider_id)
    if current and current['confirmed_at'] not in {row['confirmed_at'] for row in rows}:
        rows.append({key: current.get(key) for key in ('balance_type','credit_limit','confirmed_at')})
    return sorted(rows, key=lambda row: row['confirmed_at'])


def record_credit_valuation(conn, provider_id, balance_observation_id):
    """Pin a card's resolved value to a successful balance and existing rule.

    A later rule edit cannot recalculate this row, and a rule confirmed after
    the source read cannot create a historical value for that earlier read.
    """
    connection = _connection(conn, provider_id)
    rule = get_rule(conn, provider_id)
    if (connection is None or rule is None or connection['account_type'] != 'credit'
            or connection['excluded'] or not connection['present'] or connection['status'] != 'ACTIVE'):
        return False
    balance = conn.execute('''SELECT id,amount,currency,fetched_at FROM bank_balance_observations
        WHERE id=? AND connection_id=?''', (balance_observation_id,provider_id)).fetchone()
    if balance is None or balance['currency'] != rule['currency']:
        return False
    try:
        observed = datetime.fromisoformat(balance['fetched_at'].replace('Z', '+00:00'))
        confirmed = datetime.fromisoformat(rule['confirmed_at'].replace('Z', '+00:00'))
        if observed.tzinfo is None or confirmed.tzinfo is None or observed < confirmed:
            return False
        raw = Decimal(str(balance['amount']))
        if not raw.is_finite():
            return False
        if rule['balance_type'] == 'available_credit':
            from .currency_reporting import currency_quantum
            effective = (raw - Decimal(str(rule['credit_limit']))).quantize(currency_quantum(rule['currency']))
        else:
            effective = raw * Decimal(connection['balance_multiplier'])
    except (ValueError, TypeError, InvalidOperation):
        return False
    conn.execute('''INSERT OR IGNORE INTO bank_credit_valuations
        (balance_observation_id,connection_id,account_id,currency,effective_amount,
         balance_type,credit_limit,rule_confirmed_at,fetched_at,evidence_kind,estimated)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
        (balance['id'],provider_id,connection['account_id'],rule['currency'],float(effective),
         rule['balance_type'],rule.get('credit_limit'),rule['confirmed_at'],balance['fetched_at'],
         'confirmed_rule_at_fetch',0))
    return True


def preview_attested_credit_backfill(conn, provider_id, *, start_date, end_date,
                                     credit_limit, currency, attested_at):
    """Prepare a bounded dated valuation without altering any saved observation.

    The attestation establishes the *limit* for the stated days. Provider
    balances still have fetch timestamps rather than bank closing timestamps,
    so any resulting daily chart value remains explicitly estimated.
    """
    connection = _connection(conn, provider_id)
    if (connection is None or connection['account_type'] != 'credit' or connection['excluded']
            or connection['account_id'] is None or connection['currency'] != currency):
        raise ValueError('Choose a mapped, included credit-card connection with matching currency.')
    try:
        start = datetime.fromisoformat(start_date).date()
        end = datetime.fromisoformat(end_date).date()
        attested = datetime.fromisoformat(attested_at.replace('Z', '+00:00'))
        limit = _limit(credit_limit, currency)
        if (start.isoformat() != start_date or end.isoformat() != end_date
                or start > end or (end - start).days > 366
                or attested.tzinfo is None or attested.astimezone(timezone.utc).date() < end):
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise ValueError('Supply valid dated limit evidence and a range of at most one year.') from None
    rows = [dict(row) for row in conn.execute('''SELECT id,amount,currency,fetched_at
        FROM bank_balance_observations WHERE connection_id=?
          AND substr(fetched_at,1,10) BETWEEN ? AND ? ORDER BY fetched_at,id''',
        (provider_id,start_date,end_date))]
    if not rows or any(row['currency'] != currency for row in rows):
        raise ValueError('No matching source balances exist throughout this attested currency scope.')
    existing = {row[0] for row in conn.execute('''SELECT balance_observation_id FROM bank_credit_valuations
        WHERE connection_id=? AND substr(fetched_at,1,10) BETWEEN ? AND ?''',
        (provider_id,start_date,end_date))}
    candidates = []
    for row in rows:
        if row['id'] in existing:
            continue
        try:
            amount = Decimal(str(row['amount']))
            if not amount.is_finite():
                raise ValueError
            from .currency_reporting import currency_quantum
            effective = float((amount - Decimal(str(limit))).quantize(currency_quantum(currency)))
        except (InvalidOperation, ValueError):
            raise ValueError('A source balance in the attested scope is invalid.') from None
        candidates.append({'balance_observation_id':row['id'], 'connection_id':provider_id,
            'account_id':connection['account_id'], 'currency':currency, 'effective_amount':effective,
            'balance_type':'available_credit', 'credit_limit':limit, 'rule_confirmed_at':None,
            'fetched_at':row['fetched_at'], 'evidence_kind':'retrospective_attestation',
            'estimated':1, 'attested_at':attested_at, 'scope_start':start_date, 'scope_end':end_date})
    return {'source_balances':len(rows), 'already_recorded':len(existing),
            'candidates':candidates, 'start_date':start_date, 'end_date':end_date,
            'account_id':connection['account_id']}


def apply_attested_credit_backfill(conn, provider_id, *, start_date, end_date,
                                   credit_limit, currency, attested_at):
    """Explicit write step; caller owns review, backup and transaction."""
    preview = preview_attested_credit_backfill(conn, provider_id, start_date=start_date,
        end_date=end_date, credit_limit=credit_limit, currency=currency, attested_at=attested_at)
    for row in preview['candidates']:
        conn.execute('''INSERT INTO bank_credit_valuations
            (balance_observation_id,connection_id,account_id,currency,effective_amount,
             balance_type,credit_limit,rule_confirmed_at,fetched_at,evidence_kind,
             estimated,attested_at,scope_start,scope_end)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            tuple(row[key] for key in ('balance_observation_id','connection_id','account_id','currency',
                'effective_amount','balance_type','credit_limit','rule_confirmed_at','fetched_at',
                'evidence_kind','estimated','attested_at','scope_start','scope_end')))
    return {key: value for key, value in preview.items() if key != 'candidates'} | {
        'inserted':len(preview['candidates'])}


def set_rule(conn, provider_id, *, balance_type, credit_limit=None, currency=None):
    """Save a user-confirmed type/limit; caller owns the write transaction."""
    connection = _connection(conn, provider_id)
    if connection is None:
        raise LookupError('The connected bank account was not found.')
    if balance_type not in {'available_credit', 'reported'}:
        raise ValueError('Choose available credit or the reported balance.')
    if balance_type == 'available_credit':
        if connection['account_type'] != 'credit' or not connection['account_id'] or connection['excluded']:
            raise ValueError('Map an included credit-card account before confirming its balance type.')
        if not isinstance(currency, str) or not re.fullmatch('[A-Z]{3}', currency) or currency != connection['currency']:
            raise ValueError('The credit limit must use the connected account currency.')
        observed = conn.execute('''SELECT currency FROM bank_balance_observations
            WHERE connection_id=? ORDER BY id DESC LIMIT 1''', (provider_id,)).fetchone()
        if observed and observed['currency'] != currency:
            raise ValueError('The latest provider balance uses a different currency; review the connection first.')
        credit_limit = _limit(credit_limit, currency)
    else:
        if credit_limit is not None or currency is not None:
            raise ValueError('Restoring the reported balance does not take a credit limit or currency.')
        currency = connection['currency']
    previous = get_rule(conn, provider_id)
    confirmed = datetime.now(timezone.utc)
    if previous:
        older = datetime.fromisoformat(previous['confirmed_at'])
        if confirmed <= older:
            confirmed = older + timedelta(microseconds=1)
        _record_rule_event(conn, previous)
    confirmed_at = confirmed.isoformat()
    rule = {'balance_type': balance_type, 'provider_id': str(provider_id),
            'account_id': connection['account_id'], 'currency': currency,
            'connection_first_seen_at': connection['first_seen_at'],
            'credit_limit': credit_limit, 'confirmed_at': confirmed_at}
    conn.execute('''INSERT INTO preferences(key,value,updated_at) VALUES(?,?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at''',
        (PREFIX + str(provider_id), json.dumps(rule, allow_nan=False), confirmed_at))
    _record_rule_event(conn, rule)
    return rule


def inherit_rule(conn, provider_id, previous):
    """Bind a confirmed successor to its existing rule, retaining old evidence."""
    connection = _connection(conn, provider_id)
    rule = previous | {'provider_id': provider_id, 'account_id': connection['account_id'],
        'connection_first_seen_at': connection['first_seen_at'], 'confirmed_at': datetime.now(timezone.utc).isoformat()}
    conn.execute('INSERT INTO preferences(key,value,updated_at) VALUES(?,?,?)',
                 (PREFIX + provider_id, json.dumps(rule, allow_nan=False), rule['confirmed_at']))
    _record_rule_event(conn, rule)
    return rule


def available_credit_rule(conn, account_id, currency):
    """Resolve only the exact current mapping, never another account's rule."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bank_connections'").fetchone():
        return None
    rows = list(conn.execute('''SELECT id,balance_multiplier FROM bank_connections
        WHERE account_id=? AND currency=? AND excluded=0''', (account_id, currency)))
    if len(rows) != 1:
        return None
    rule = get_rule(conn, rows[0]['id'])
    if rule is None or rule['balance_type'] != 'available_credit' or rows[0]['balance_multiplier'] not in (-1, 1):
        return None
    return rule | {'balance_multiplier': rows[0]['balance_multiplier']}

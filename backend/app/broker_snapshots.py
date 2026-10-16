"""Validate imported broker history against independent statement snapshots.

A late monthly statement proves its as-of positions and cash, not acquisition
cost or all earlier activity. This helper never manufactures opening trades.
"""
from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

ZERO = Decimal(0)
EPS = Decimal('0.00000001')


def _number(value):
    try:
        n = Decimal(str(value))
        return n if n.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _meta(raw):
    try:
        value = json.loads(raw or '{}')
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _quantities(positions, field='shares'):
    result = {}
    for row in positions or []:
        key = row.get('isin') or row.get('ticker')
        number = _number(row.get(field))
        if not key or number is None or key in result:
            return None
        result[key] = number
    return result


def _cash(values):
    result = {k:_number(v) for k,v in (values or {}).items()}
    return None if any(v is None for v in result.values()) else result


def _same(left, right, tolerance=EPS):
    return (left is not None and right is not None
            and all(abs(left.get(k,ZERO)-right.get(k,ZERO)) <= tolerance for k in left.keys() | right.keys()))


def broker_snapshot_status(conn, account_ids=None):
    """Return per-account Moomoo snapshot/evidence; no writes or market calls.

    ``use_snapshot_fallback`` is authoritative even if ``fallback_positions``
    is empty. Callers should replace that account's reconstructed positions
    with the as-of snapshot and keep every historical cost/P&L unknown. Native
    cash is independently supplied in ``cash_balances`` with the same date.
    Other brokers are untouched; ``None`` selects all, ``[]`` selects none.
    """
    if account_ids == []:
        return {}
    requested = None if account_ids is None else set(account_ids)
    grouped = defaultdict(list)
    for record in conn.execute("SELECT s.*,a.bank FROM statements s JOIN accounts a ON a.id=s.account_id WHERE a.account_type='investment' ORDER BY s.period_end,s.id"):
        row = dict(record)
        if requested is not None and row['account_id'] not in requested:
            continue
        meta = _meta(row['meta'])
        if meta.get('parser') == 'moomoo':
            grouped[row['account_id']].append((row,meta))
    result = {}
    for account, statements in grouped.items():
        warnings = []
        def warn(message):
            if message not in warnings:
                warnings.append(message)
        events = defaultdict(list)
        unassigned = []
        for row in conn.execute('SELECT txn_date,meta FROM transactions WHERE account_id=?',(account,)):
            meta = _meta(row['meta'])
            period = meta.get('source_statement_end')
            if period:
                events[period].append(meta)
            else:
                unassigned.append(meta)
        if unassigned:
            warn('Some investment events have no broker statement period; their relationship to the snapshot is unverified.')
        previous = None
        chain_complete = True
        share_matches = cash_matches = True
        aggregate_shares = defaultdict(lambda: ZERO)
        aggregate_cash = defaultdict(lambda: ZERO)
        seen_dates = {}
        for row,meta in statements:
            day = meta.get('snapshot_date') or row['period_end']
            closing_positions = _quantities(meta.get('positions'))
            opening_positions = _quantities(meta.get('position_changes'),'starting_shares')
            closing_cash = _cash(meta.get('cash_balances'))
            opening_cash = _cash(meta.get('opening_cash_balances'))
            signature = (closing_positions,opening_positions,closing_cash,opening_cash)
            if day in seen_dates:
                if seen_dates[day] != signature:
                    chain_complete = False
                    warn('Conflicting snapshots exist for the same statement date.')
                continue
            seen_dates[day] = signature
            if not day or meta.get('native_reconciled') is not True or any(v is None for v in signature):
                chain_complete = False
                warn('A broker snapshot lacks independently reconciled cash or position evidence.')
            if previous is None:
                if not _same(opening_positions,{}) or not _same(opening_cash,{},ZERO):
                    chain_complete = False
                    warn('Imported history begins with existing shares or cash. Earlier purchases and costs are missing.')
            else:
                previous_day,previous_positions,previous_cash = previous
                if meta.get('previous_snapshot_date') != previous_day:
                    chain_complete = False
                    warn('There is a gap between imported monthly statements.')
                if not _same(opening_positions,previous_positions) or not _same(opening_cash,previous_cash,ZERO):
                    chain_complete = False
                    warn('Adjacent statements disagree on carried shares or cash.')
            month_shares = defaultdict(lambda: ZERO)
            month_cash = defaultdict(lambda: ZERO)
            valid = True
            for event in events[day]:
                shares = _number(event.get('shares') or 0)
                key = event.get('isin') or event.get('ticker')
                if shares is None or (shares and not key):
                    valid = False
                elif key:
                    month_shares[key] += shares
                    aggregate_shares[key] += shares
                for ccy,amount in (event.get('cash_movements') or {}).items():
                    native = _number(amount)
                    if native is None:
                        valid = False
                    else:
                        month_cash[ccy] += native
                        aggregate_cash[ccy] += native
            expected_shares = ({k:(opening_positions or {}).get(k,ZERO)+month_shares[k]
                                for k in (opening_positions or {}).keys() | month_shares.keys()})
            expected_cash = ({k:(opening_cash or {}).get(k,ZERO)+month_cash[k]
                              for k in (opening_cash or {}).keys() | month_cash.keys()})
            if not valid or not _same(expected_shares,closing_positions):
                share_matches = False
                warn('Stored share events do not reconcile with a monthly broker snapshot.')
            if not valid or not _same(expected_cash,closing_cash,ZERO):
                cash_matches = False
                warn('Stored cash events do not reconcile with a monthly broker snapshot.')
            previous = day,closing_positions,closing_cash
        latest_row,latest = statements[-1]
        closing_positions = _quantities(latest.get('positions'))
        closing_cash = _cash(latest.get('cash_balances'))
        share_matches = share_matches and _same(dict(aggregate_shares),closing_positions)
        cash_matches = cash_matches and _same(dict(aggregate_cash),closing_cash,ZERO)
        if not share_matches:
            warn('Imported activity cannot reconstruct the latest statement quantities from a zero opening.')
        if not cash_matches:
            warn('Imported activity cannot reconstruct the latest statement cash from a zero opening.')
        unknown_periods = set(events)-set(seen_dates)
        if unknown_periods:
            chain_complete = False
            warn('Some stored investment events have no corresponding imported monthly statement.')
        complete = chain_complete and share_matches and cash_matches and not unassigned
        fallback = []
        if not complete:
            warn('Use the broker statement quantities and cash as of its date. Acquisition costs and profit remain unknown; import the missing history to calculate them.')
            for position in latest.get('positions',[]):
                fallback.append({**position,'account_id':account,'net_shares':position['shares'],
                    'as_of':latest.get('snapshot_date') or latest_row['period_end'],
                    'quantity_source':'broker_statement_snapshot',
                    'cost_basis_gbp':None,'cost_basis_native':None,'estimated_cost_basis_gbp':None,
                    'realised_gbp':None,'unrealised_gbp':None,
                    'historical_cost_complete':False,'history_complete':False})
        result[account] = {'account_id':account,'statement_id':latest_row['id'],
            'source_file':Path(latest_row['source_file']).name,
            'snapshot_date':latest.get('snapshot_date') or latest_row['period_end'],
            'positions':latest.get('positions',[]),'cash_balances':latest.get('cash_balances',{}),
            'base_currency':latest.get('base_currency'),'net_asset_value':latest.get('net_asset_value'),
            'portfolio_value':latest.get('portfolio_value'),
            'history_complete':complete,'share_history_matches_snapshot':share_matches,
            'cash_history_matches_snapshot':cash_matches,'use_snapshot_fallback':not complete,
            'fallback_positions':fallback,'warnings':warnings}
    return result

"""Source-backed position and moving-average cost replay, independent of quotes.

GBP book cost uses historical GBP cash consideration only. A parallel ledger in
the execution currency explains trades whose historical GBP conversion is absent.
Broker-reported results are kept separate from our fee-inclusive book accounting.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

ZERO = Decimal(0)
EPS = Decimal('0.00000001')


def decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = Decimal(str(value))
        return value if value.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def major_currency(currency):
    value = str(currency or '').strip()
    return ('GBP', Decimal('.01')) if value in {'GBX', 'GBp'} else (value.upper(), Decimal(1))


def _time(row, meta):
    try:
        value = datetime.fromisoformat(str(meta.get('time') or row['txn_date']).replace('Z', '+00:00'))
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _trade_value(row, meta, buy, native_currency=None):
    field = 'cost_basis_gbp' if buy else 'proceeds_gbp'
    currency, scale = major_currency(meta.get('native_currency') or meta.get('currency') or 'GBP')
    cash = decimal(meta.get('native_amount'))
    if native_currency is None:
        if field in meta:
            return decimal(meta[field])
        return abs(decimal(row['amount'])) if currency == 'GBP' else None
    native_field = 'cost_basis_native' if buy else 'proceeds_native'
    if native_field in meta and meta.get('execution_currency_major') == native_currency:
        return decimal(meta[native_field])
    if currency == native_currency:
        if cash is not None:
            return abs(cash) * scale
        if currency == 'GBP':
            return _trade_value(row, meta, buy)
    price_currency, price_scale = major_currency(meta.get('execution_price_currency'))
    rate = decimal(meta.get('exchange_rate'))
    # The printed rate expresses price-currency units per Total-currency unit.
    # Total already includes source fees; multiplying converts those fees once.
    if cash is not None and price_currency == native_currency and rate is not None and rate > 0:
        return abs(cash) * rate * price_scale
    return None


def _new_position(account, isin, ticker, native_currency):
    return {'account_id': account, 'isin': isin, 'ticker': ticker,
            'net_shares': ZERO, 'bought_shares': ZERO, 'sold_shares': ZERO,
            'corporate_action_shares': ZERO, 'gift_shares': ZERO, 'remaining_cost': ZERO,
            'realised': ZERO, 'cost_basis_complete': True, 'realised_complete': True,
            'remaining_native_cost': ZERO, 'native_realised': ZERO,
            'native_cost_complete': True, 'native_realised_complete': True,
            'estimated_remaining_cost': ZERO, 'estimated_realised': ZERO,
            'estimated_cost_complete': True, 'estimated_realised_complete': True,
            'estimated_bought': ZERO, 'estimated_sold': ZERO,
            'estimated_bought_complete': True, 'estimated_sold_complete': True,
            'estimated_trade_count': 0, 'reference_fx': {},
            'native_currency': native_currency, 'position_complete': True,
            'execution_price_currency': None,
            'bought': ZERO, 'sold': ZERO, 'bought_complete': True, 'sold_complete': True,
            'native_bought': ZERO, 'native_sold': ZERO,
            'native_bought_complete': True, 'native_sold_complete': True,
            'reported_results': defaultdict(lambda: ZERO), 'reported_result_count': 0,
            'trades': 0, 'first': None, 'last': None, 'corporate_actions': [],
            'warnings': set(), 'missing_gbp_trade_ids': [], 'trade_ids': [],
            'account_ids': [], 'history_complete': True, 'snapshot_sources': []}


def holding_positions(conn, account_ids=None):
    from .investment_accounts import scope_sql
    scope, params = scope_sql(account_ids)
    try:
        reference_fx = {(r['currency'], r['date']): dict(r) for r in conn.execute('SELECT * FROM reference_fx')}
    except Exception as error:
        import sqlite3
        if not isinstance(error, sqlite3.OperationalError) or 'no such table' not in str(error):
            raise
        reference_fx = {}
    parsed = []
    candidates = defaultdict(Counter)
    for row in conn.execute("SELECT t.id,t.account_id,t.merchant,t.amount,t.meta,t.txn_date,t.type_code "
                            "FROM transactions t JOIN accounts a ON a.id=t.account_id "
                            "WHERE a.account_type='investment' AND t.meta IS NOT NULL" + scope, params):
        row = dict(row)
        try:
            meta = json.loads(row['meta'])
        except (TypeError, ValueError):
            continue
        if not isinstance(meta, dict) or not meta.get('isin'):
            continue
        shares = decimal(meta.get('shares'))
        if shares is None or shares == 0:
            continue  # Dividend eligibility units and results are not trades.
        parsed.append((row, meta, None))
        ccy, _ = major_currency(meta.get('execution_currency_major') or meta.get('execution_price_currency')
                                or meta.get('native_currency') or meta.get('currency') or 'GBP')
        candidates[(row['account_id'], meta['isin'])][ccy] += 1
    source_splits = {(r['account_id'], m['isin'], r['txn_date']) for r, m, _ in parsed if r['type_code'] == 'Stock split'}
    for source in conn.execute('SELECT * FROM corporate_actions WHERE ex_date<=?', (date.today().isoformat(),)):
        action = dict(source)
        key = (action['account_id'], action['isin'])
        if key in candidates and (*key, action['ex_date']) not in source_splits:
            parsed.append(({'id': action['id'], 'account_id': key[0], 'txn_date': action['ex_date'], 'merchant': None},
                           {'isin': key[1]}, action))
    parsed.sort(key=lambda item: (item[0]['account_id'], _time(item[0], item[1]),
                                 0 if item[2] else 1, item[0]['id']))
    lots = {}
    for row, meta, action in parsed:
        key = (row['account_id'], meta['isin'])
        p = lots.setdefault(key, _new_position(*key, row['merchant'], candidates[key].most_common(1)[0][0]))
        if action:
            before = p['net_shares']
            if abs(before) > EPS:
                p['net_shares'] *= Decimal(action['ratio_new']) / Decimal(action['ratio_old'])
                p['corporate_action_shares'] += p['net_shares'] - before
                p['corporate_actions'].append({**action, 'before_shares': float(before), 'after_shares': float(p['net_shares'])})
            continue
        p['ticker'] = meta.get('ticker') or row['merchant'] or p['ticker']
        p['execution_price_currency'] = meta.get('execution_price_currency') or p['execution_price_currency']
        shares = decimal(meta['shares'])
        if row['type_code'] == 'Stock split':
            ratio = decimal(meta.get('split_ratio'))
            before = p['net_shares']
            expected = before * (ratio - 1) if ratio is not None and ratio > 0 else None
            if expected is None or abs(expected - shares) > EPS:
                p['position_complete'] = False
                p['warnings'].add('Printed split units do not reconcile with prior holdings; review missing activity.')
            p['net_shares'] += shares
            p['corporate_action_shares'] += shares
            p['corporate_actions'].append({'id': row['id'], 'ex_date': row['txn_date'],
                'ratio_new': float(ratio) if ratio else None, 'ratio_old': 1,
                'source_url': None, 'source_title': 'Imported broker statement stock split',
                'before_shares': float(before), 'after_shares': float(p['net_shares'])})
            continue
        if row['type_code'] == 'Stock gift':
            p['net_shares'] += shares
            p['gift_shares'] += shares
            p['cost_basis_complete'] = p['native_cost_complete'] = p['estimated_cost_complete'] = False
            p['warnings'].add('Gifted shares have no acquisition cost in the statement. Cost and profit remain unknown until that basis is supplied.')
            p['first'] = min(p['first'] or row['txn_date'], row['txn_date'])
            p['last'] = max(p['last'] or '', row['txn_date'])
            continue
        buy, before = shares > 0, p['net_shares']
        gbp = _trade_value(row, meta, buy)
        native = _trade_value(row, meta, buy, p['native_currency'])
        estimated = gbp
        if estimated is None:
            cash_currency, cash_scale = major_currency(meta.get('native_currency'))
            cash = decimal(meta.get('native_amount'))
            reference = reference_fx.get((cash_currency, row['txn_date']))
            rate = decimal((reference or {}).get('rate_to_gbp'))
            if cash is not None and rate is not None and rate > 0:
                estimated = abs(cash) * cash_scale * rate
                p['estimated_trade_count'] += 1
                p['reference_fx'][(cash_currency, row['txn_date'])] = reference
        side = 'bought' if buy else 'sold'
        p[side + '_shares'] += abs(shares)
        p['trades'] += 1
        p['trade_ids'].append(row['id'])
        if gbp is None or gbp < 0:
            gbp = None
            p[side + '_complete'] = False
            p['missing_gbp_trade_ids'].append(row['id'])
        else:
            p[side] += gbp
        if native is None or native < 0:
            native = None
            p['native_' + side + '_complete'] = False
        else:
            p['native_' + side] += native
        if estimated is None or estimated < 0:
            estimated = None
            p['estimated_' + side + '_complete'] = False
        else:
            p['estimated_' + side] += estimated
        if not row['txn_date']:
            p['position_complete'] = False
            p['warnings'].add('A trade has no reliable date.')
        if not buy and (before <= 0 or -shares > before + EPS):
            p['position_complete'] = False
            p['warnings'].add('A sale exceeds the units reconstructed before it; opening units or a corporate action may be missing.')
        for value, cost_key, complete_key, result_key, result_complete in (
                (gbp, 'remaining_cost', 'cost_basis_complete', 'realised', 'realised_complete'),
                (native, 'remaining_native_cost', 'native_cost_complete', 'native_realised', 'native_realised_complete'),
                (estimated, 'estimated_remaining_cost', 'estimated_cost_complete', 'estimated_realised', 'estimated_realised_complete')):
            if buy:
                if value is None or not p['position_complete']:
                    p[complete_key] = False
                if value is not None:
                    p[cost_key] += value
            else:
                removed = p[cost_key] * min(-shares / before, Decimal(1)) if before > 0 else ZERO
                if not p['position_complete']:
                    p[complete_key] = False
                if p[complete_key] and value is not None:
                    p[result_key] += value - removed
                else:
                    p[result_complete] = False
                p[cost_key] -= removed
        p['net_shares'] += shares
        if abs(p['net_shares']) < EPS:
            p['net_shares'] = p['remaining_cost'] = p['remaining_native_cost'] = p['estimated_remaining_cost'] = ZERO
            p['cost_basis_complete'] = p['native_cost_complete'] = p['estimated_cost_complete'] = p['position_complete']
        if not buy:
            result = decimal(meta.get('result_native', meta.get('result')))
            currency, scale = major_currency(meta.get('result_currency') or ('GBP' if meta.get('result') is not None else ''))
            if result is not None and currency:
                p['reported_results'][currency] += result * scale
                p['reported_result_count'] += 1
        p['first'] = min(p['first'] or row['txn_date'] or '', row['txn_date'] or '')
        p['last'] = max(p['last'] or '', row['txn_date'] or '')
    # A monthly statement can prove units without proving the purchases that
    # produced them. Use that evidence without inventing an opening trade/cost.
    from .broker_snapshots import broker_snapshot_status
    for account, status in broker_snapshot_status(conn, account_ids).items():
        if not status['use_snapshot_fallback']:
            continue
        snapshot = {row['isin']: row for row in status['fallback_positions'] if row.get('isin')}
        keys = {key for key in lots if key[0] == account} | {(account, isin) for isin in snapshot}
        for key in keys:
            source = snapshot.get(key[1])
            if key not in lots:
                lots[key] = _new_position(account, key[1], source['ticker'], source['currency'])
            p = lots[key]
            p['net_shares'] = decimal(source['net_shares']) if source else ZERO
            p['history_complete'] = False
            p['position_complete'] = True  # The dated snapshot independently proves units.
            for field in ('cost_basis_complete', 'native_cost_complete', 'estimated_cost_complete',
                          'realised_complete', 'native_realised_complete', 'estimated_realised_complete'):
                p[field] = False
            p['snapshot_sources'].append({'account_id': account, 'date': status['snapshot_date'],
                'source_file': status['source_file'], 'net_shares': float(p['net_shares'])})
            p['warnings'].update(status['warnings'])
    positions = {}
    for p in lots.values():
        p['account_ids'] = [p['account_id']]
        combined = positions.setdefault(p['isin'], _new_position(None, p['isin'], p['ticker'], p['native_currency']))
        if combined['native_currency'] != p['native_currency']:
            combined['native_currency'] = None
            combined['native_cost_complete'] = combined['native_realised_complete'] = False
            combined['native_bought_complete'] = combined['native_sold_complete'] = False
        for key, value in p.items():
            if isinstance(value, Decimal):
                combined[key] += value
            elif isinstance(value, bool):
                combined[key] &= value
        for key in ('trades', 'reported_result_count', 'estimated_trade_count'):
            combined[key] += p[key]
        combined['reference_fx'].update(p['reference_fx'])
        if not combined['last'] or (p['last'] or '') >= combined['last']:
            combined['execution_price_currency'] = p['execution_price_currency']
            combined['ticker'] = p['ticker']
        for key in ('corporate_actions', 'missing_gbp_trade_ids', 'trade_ids', 'account_ids', 'snapshot_sources'):
            combined[key].extend(p[key])
        combined['warnings'].update(p['warnings'])
        for currency, value in p['reported_results'].items():
            combined['reported_results'][currency] += value
        combined['first'] = min(combined['first'] or p['first'] or '', p['first'] or '')
        combined['last'] = max(combined['last'] or '', p['last'] or '')
    for p in positions.values():
        shares = p['net_shares']
        p['cost_basis'] = round(float(p['remaining_cost']), 2) if p['cost_basis_complete'] else None
        p['avg_cost'] = round(float(p['remaining_cost'] / shares), 6) if shares > 0 and p['cost_basis_complete'] else (0.0 if p['cost_basis_complete'] else None)
        p['native_cost_basis'] = round(float(p['remaining_native_cost']), 2) if p['native_cost_complete'] else None
        p['native_avg_cost'] = round(float(p['remaining_native_cost'] / shares), 6) if shares > 0 and p['native_cost_complete'] else (0.0 if p['native_cost_complete'] else None)
        p['realised_known'] = round(float(p['realised']), 2)
        p['realised'] = p['realised_known'] if p['realised_complete'] else None
        p['native_realised_known'] = round(float(p['native_realised']), 2)
        p['native_realised'] = p['native_realised_known'] if p['native_realised_complete'] else None
        p['estimated_cost_basis'] = round(float(p['estimated_remaining_cost']), 2) if p['estimated_cost_complete'] else None
        p['estimated_avg_cost'] = round(float(p['estimated_remaining_cost'] / shares), 6) if shares > 0 and p['estimated_cost_complete'] else (0.0 if p['estimated_cost_complete'] else None)
        p['estimated_realised_known'] = round(float(p['estimated_realised']), 2)
        p['estimated_realised'] = p['estimated_realised_known'] if p['estimated_realised_complete'] else None
        p['reference_fx'] = list(p['reference_fx'].values())
        p['reported_results'] = [{'currency': c, 'amount': round(float(v), 2)} for c, v in sorted(p['reported_results'].items())]
        p['warnings'] = sorted(p['warnings'])
        for side in ('bought', 'sold', 'native_bought', 'native_sold', 'estimated_bought', 'estimated_sold'):
            p[side + '_known'] = round(float(p[side]), 2)
            p[side] = p[side + '_known'] if p[side + '_complete'] else None
        for key, value in list(p.items()):
            if isinstance(value, Decimal):
                p[key] = round(float(value), 10)
    return positions

"""Read-only, source-backed fill history for the portfolio drill-down."""
import json
from pathlib import Path

from .parsers.trading212 import BUY, SELL


def trade_history(conn, isin: str, limit: int = 100, offset: int = 0, account_ids=None):
    from .investment_accounts import scope_sql
    scope, params = scope_sql(account_ids)
    trades = []
    for row in conn.execute("""SELECT t.id,t.account_id,t.txn_date,t.type_code,t.amount,t.meta,
            t.source_merchant,t.merchant,s.source_file,a.display_name
        FROM transactions t JOIN accounts a ON a.id=t.account_id
        LEFT JOIN statements s ON s.id=t.statement_id
        WHERE a.account_type='investment'""" + scope, params):
        if row['type_code'] not in BUY | SELL:
            continue
        try:
            meta = json.loads(row['meta'] or '{}')
        except (ValueError, TypeError):
            continue
        if meta.get('isin') != isin:
            continue
        sources = {Path(row['source_file']).name} if row['source_file'] else set()
        for source in meta.get('source_observations', []):
            if source.get('source_file'):
                sources.add(Path(source['source_file']).name)
        trades.append({
            'id': row['id'], 'account': row['display_name'], 'account_id': row['account_id'], 'date': row['txn_date'],
            'time': meta.get('time'), 'time_timezone': meta.get('time_timezone'),
            'action': row['type_code'], 'ticker': meta.get('ticker') or row['source_merchant'] or row['merchant'],
            'shares': meta.get('shares'),
            'price': meta.get('execution_price'), 'price_currency': meta.get('execution_price_currency'),
            'price_major': meta.get('execution_price_major'), 'currency_major': meta.get('execution_currency_major'),
            'cash_amount': meta.get('native_amount', row['amount']),
            'cash_currency': meta.get('native_currency') or 'GBP',
            'gbp_cash_amount': row['amount'] if meta.get('gbp_value_known', True) else None,
            'exchange_rate': meta.get('exchange_rate'), 'fees': meta.get('fees', []),
            'broker_result': meta.get('result_native'), 'broker_result_currency': meta.get('result_currency'),
            'sources': sorted(sources),
        })
    trades.sort(key=lambda t: (t['time'] or t['date'] or '', t['id']), reverse=True)
    return {'isin': isin, 'total': len(trades), 'offset': offset, 'limit': limit,
            'trades': trades[offset:offset + limit]}

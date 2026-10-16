"""One explicit account scope shared by every portfolio calculation."""
import json


def available_accounts(conn):
    from .broker_snapshots import broker_snapshot_status
    snapshots = broker_snapshot_status(conn)
    result = []
    for row in conn.execute("SELECT id,display_name,bank FROM accounts WHERE account_type='investment' ORDER BY bank,id"):
        account = dict(row)
        dates = conn.execute('SELECT MIN(period_start),MAX(period_end) FROM statements WHERE account_id=?', (row['id'],)).fetchone()
        latest = conn.execute('SELECT meta FROM statements WHERE account_id=? ORDER BY period_end DESC,id DESC LIMIT 1', (row['id'],)).fetchone()
        meta = json.loads(latest['meta'] or '{}') if latest else {}
        account.update(history_start=dates[0], history_end=dates[1], statement_asof=dates[1],
                       history_complete=meta.get('history_complete'), history_note=None)
        if row['id'] in snapshots:
            status = snapshots[row['id']]
            account.update(history_complete=status['history_complete'],
                           history_note=' '.join(status['warnings']) or None)
        if meta.get('parser') == 'moomoo':
            account['statement_snapshot'] = {
                'date': meta.get('snapshot_date'), 'base_currency': meta.get('base_currency'),
                'net_asset_value': meta.get('net_asset_value'),
                'securities_value': meta.get('portfolio_value'),
                'cash_balances': meta.get('cash_balances', {}),
                'n_positions': len(meta.get('positions', [])),
                'reconciled': meta.get('native_reconciled', False),
            }
        result.append(account)
    return result


def selected_accounts(conn, account_ids=None):
    """None is all; an empty list is deliberately none. Reject unknown IDs."""
    available = [a['id'] for a in available_accounts(conn)]
    if account_ids is None:
        return available
    requested = list(dict.fromkeys(account_ids))
    if requested == ['__none__']:
        return []
    if len(requested) > 50 or any(item not in available for item in requested):
        raise ValueError('Choose valid investment accounts from the account selector.')
    return requested


def scope_sql(account_ids, column='t.account_id'):
    if account_ids is None:
        return '', []
    if not account_ids:
        return ' AND 0', []
    return f" AND {column} IN ({','.join('?' for _ in account_ids)})", list(account_ids)

"""Saved security exposure and account overlap; no provider or trading calls.

The pure projection uses known gross security values, never account NAV or a
fund's unobserved constituents. Exact provider instruments stay distinct.
"""
from collections import Counter, defaultdict
from datetime import datetime, timezone

from . import investing_analytics, market_cache, snaptrade_store
from .snaptrade_models import number


def _identity(position):
    expected = market_cache.listing(position)
    if not expected:
        return None
    return (expected['symbol'], expected['currency'], tuple(sorted(expected['exchanges'])), expected['asset_type'])


def calculate(analysis, cached):
    """Project reviewed values without I/O or rounding intermediate weights."""
    selected = set(analysis['selected_account_ids'])
    accounts = [a for a in cached.get('accounts', []) if a['id'] in selected]
    rows = [r for r in analysis['holdings'] if not r['is_cash']]
    account_counts = Counter(a['id'] for a in accounts)
    key_counts = Counter((a['id'], p.get('id')) for a in accounts
                         for p in a.get('positions', {}).get('data', []) if not p.get('is_cash'))
    duplicate_accounts = {key for key, count in account_counts.items() if count > 1}
    duplicate_positions = {key for key, count in key_counts.items() if count > 1}
    raw = {(a['id'], p.get('id')): p for a in accounts
           for p in a.get('positions', {}).get('data', []) if not p.get('is_cash')}
    identities = defaultdict(set)
    for account in cached.get('accounts', []):
        for position in account.get('positions', {}).get('data', []):
            if not position.get('is_cash') and position.get('instrument_id'):
                identities[position['instrument_id']].add(_identity(position))
    conflicting = {key for key, values in identities.items() if len(values) > 1}
    bad_accounts = set(duplicate_accounts) | {key[0] for key in duplicate_positions}
    invalid_identity, missing_values, grouped = 0, 0, defaultdict(list)
    for row in rows:
        key = (row['account_id'], row.get('position_id'))
        position = raw.get(key, {})
        if (not row.get('position_id') or not row.get('instrument_id')
                or _identity(position) is None or row['instrument_id'] in conflicting):
            invalid_identity += 1
            bad_accounts.add(row['account_id'])
            continue
        grouped[row['instrument_id']].append(row)
        if number(row.get('value')) is None:
            missing_values += 1
            bad_accounts.add(row['account_id'])
    duplicate_keys = bool(duplicate_accounts or duplicate_positions)
    known_rows = [r for r in rows if number(r.get('value')) is not None]
    cards = {a['id']: a for a in analysis['accounts'] if a['selected']}
    supported_empty = bool(accounts) and not rows and all(
        account.get('positions', {}).get('status') in {'ok', 'empty'}
        and not account.get('positions', {}).get('truncated')
        and account.get('initial_sync_complete') is not False
        and not account.get('holdings_unavailable')
        and number(account.get('total_value')) is not None
        and not cards[account['id']].get('stale')
        for account in accounts)
    gross = (sum(abs(r['value']) for r in known_rows) if known_rows else 0.0 if supported_empty else None) if not duplicate_keys else None
    reliable = bool(gross and not invalid_identity and not duplicate_keys)
    warnings = []
    if duplicate_keys:
        warnings.append('Repeated account or position identifiers make aggregation ambiguous. Combined values, concentration, overlap and scenarios are withheld.')
    if invalid_identity:
        warnings.append('Some holdings lack a verified listing identity or share a conflicting instrument ID. Concentration and scenarios are withheld; only verified instruments are listed.')
    incomplete_accounts = []
    for account in accounts:
        section = account.get('positions', {})
        if section.get('status') not in {'ok', 'empty'} or section.get('truncated'):
            incomplete_accounts.append(account['id'])
            bad_accounts.add(account['id'])
    missing_values = sum(number(r.get('value')) is None for r in rows)
    if missing_values or incomplete_accounts:
        warnings.append('Unavailable holding values or account holdings are excluded. Figures describe the known securities only; affected account overlap is withheld.')
    if any(r.get('stale') for r in rows):
        warnings.append('Some quantities or values are from an older saved account observation.')
    instruments, account_weights = [], defaultdict(dict)
    for identifier, positions in grouped.items():
        if duplicate_keys:
            break
        position = raw[(positions[0]['account_id'], positions[0]['position_id'])]
        expected = market_cache.listing(position)
        per_account = []
        for account_id in dict.fromkeys(r['account_id'] for r in positions):
            subset = [r for r in positions if r['account_id'] == account_id]
            known = [r['value'] for r in subset if number(r.get('value')) is not None]
            amount = sum(abs(value) for value in known) if known else None
            if amount is not None:
                account_weights[account_id][identifier] = amount
            card = cards[account_id]
            per_account.append({'id': account_id, 'name': card.get('name'), 'institution': card.get('institution'),
                                'gross_value': amount, 'positions': len(subset)})
        known = [r['value'] for r in positions if number(r.get('value')) is not None]
        amount = sum(abs(value) for value in known) if known else None
        instruments.append({'instrument_id': identifier, 'symbol': expected['symbol'], 'name': positions[0]['name'],
            'listing_currency': expected['currency'], 'exchange': position.get('exchange'),
            'asset_type': expected['asset_type'], 'identity_verified': True,
            'gross_value': amount, 'net_value': sum(known) if known else None,
            'weight_pct': 100 * amount / gross if reliable and amount is not None else None,
            'missing_values': len(positions) - len(known), 'accounts': per_account})
    instruments.sort(key=lambda row: (row['gross_value'] is None, -(row['gross_value'] or 0), row['symbol'], row['instrument_id']))
    weights = [row['gross_value'] / gross for row in instruments if row['gross_value'] is not None] if reliable else []
    hhi = sum(weight ** 2 for weight in weights) if weights else None
    overlaps = []
    funded = [(key, values) for key, values in account_weights.items() if sum(values.values()) > 0]
    for index, (first_id, first) in enumerate(funded):
        for second_id, second in funded[index + 1:]:
            valid = first_id not in bad_accounts and second_id not in bad_accounts
            first_total, second_total = sum(first.values()), sum(second.values())
            overlap = sum(min(first.get(key, 0) / first_total, second.get(key, 0) / second_total)
                          for key in first.keys() | second.keys()) * 100 if valid else None
            overlaps.append({'first_account_id': first_id, 'first_account_name': cards[first_id]['name'],
                             'second_account_id': second_id, 'second_account_name': cards[second_id]['name'],
                             'overlap_pct': overlap,
                             'shared_instruments': sum(first.get(key, 0) > 0 and second.get(key, 0) > 0 for key in first.keys() & second.keys()) if valid else None,
                             'reason': None if valid else 'Incomplete values or ambiguous identities prevent this comparison.'})
    long_only = reliable and not any(row['value'] < 0 or (number(row.get('quantity')) or 0) < 0 for row in known_rows)
    largest = weights[0] if weights else None
    top5 = sum(weights[:5]) if weights else None
    if reliable and not long_only:
        warnings.append('Price-fall scenarios are withheld because short positions need signed exposure treatment.')
    return {'currency': analysis['currency'], 'selected_account_ids': analysis['selected_account_ids'],
        'paper': analysis['paper'], 'retrieved_at': analysis.get('retrieved_at'),
        'status': 'unavailable' if not reliable else 'partial' if warnings else 'ready',
        'summary': {'gross_security_value': gross, 'holdings': len(rows), 'known_holdings': len(known_rows),
                    'instruments': len(instruments), 'selected_accounts': len(accounts), 'accounts_with_securities': len(funded),
                    'largest_weight_pct': largest * 100 if largest is not None else None,
                    'top5_weight_pct': top5 * 100 if top5 is not None else None,
                    'hhi_fraction': hhi, 'effective_instruments': 1 / hhi if hhi else None},
        'instruments': instruments, 'account_overlap': overlaps,
        'scenarios': {'shock_pct': -10, 'largest_gross_impact_pct': -10 * largest if long_only else None,
                      'top5_gross_impact_pct': -10 * top5 if long_only else None,
                      'largest_value_change': -.1 * gross * largest if long_only else None,
                      'top5_value_change': -.1 * gross * top5 if long_only else None},
        'warnings': warnings, 'methodology': [
            'Weights and scenarios use known gross absolute security values in the selected currency. Cash and unavailable holdings are excluded. This denominator is not account value or total wealth.',
            'Only matching provider instrument IDs with consistent verified listings combine across accounts. Matching tickers alone never combine.',
            'HHI is the sum of squared instrument weights; its inverse is the number of equally weighted instruments with the same concentration. This does not measure correlated risk.',
            'Account overlap sums the smaller normalized gross weight for each shared instrument. Each account must have complete, identified holding values; accounts with zero securities have no percentage overlap.',
            'Scenarios reduce the largest instrument or the five largest by 10%, holding all other prices, quantities and FX fixed. They are static long-only scenarios, not forecasts or personal returns.',
            'Funds are single instruments. No underlying holdings, issuer aggregation or fund look-through is inferred.',
        ]}


def bundle(conn, *, path=None, currency_code='GBP', account_ids=None, paper=False, today=None, cached=None, markets=None):
    today = today or datetime.now(timezone.utc).date()
    if cached is None:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='snaptrade_cache'").fetchone()
        cached = snaptrade_store._load(conn) if exists else None
    cached = cached or {'accounts': [], 'connections': {'data': []}}
    markets = market_cache.read_market_data(conn) if markets is None else markets
    analysis = investing_analytics.bundle(conn, currency_code=currency_code, account_ids=account_ids,
                                         paper=paper, today=today, cached=cached, markets=markets)
    return calculate(analysis, cached)

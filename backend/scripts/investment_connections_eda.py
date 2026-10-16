#!/usr/bin/env python3
"""Read-only investment EDA; emits aggregates, never identities or money.

python backend/scripts/investment_connections_eda.py --db /private/snapshot.db
The database is opened mode=ro with query_only; no provider calls or migrations.
Concentration describes saved security exposure, not personal performance.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import investing_analytics, investment_profile, market_cache, snaptrade_store


def analyze(path, today):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    try:
        conn.execute('BEGIN')
        cached = snaptrade_store._load(conn)
        if not cached:
            raise ValueError('No saved connected investment portfolio.')
        markets = market_cache.read_market_data(conn)
        analysis = investing_analytics.bundle(conn, cached=cached, markets=markets, today=today)
        result = investment_profile.profile(cached, analysis, today=today)
        accounts = [a for a in cached['accounts'] if a['id'] in analysis['selected_account_ids']]
        raw = [p for a in accounts for p in a['positions']['data'] if not p.get('is_cash')]
        rows = [p for p in analysis['holdings'] if not p['is_cash']]
        valid = investment_profile.numeric
        gross = sum(abs(r['value']) for r in rows if valid(r['value']))
        instrument_values, by_account = defaultdict(float), defaultdict(dict)
        for row in rows:
            if valid(row['value']) and row.get('instrument_id'):
                instrument_values[row['instrument_id']] += abs(row['value'])
                values = by_account[row['account_id']]
                values[row['instrument_id']] = values.get(row['instrument_id'], 0) + abs(row['value'])
        covered = sum(instrument_values.values())
        weights = sorted((value / gross for value in instrument_values.values()), reverse=True) if gross else []
        complete = bool(gross and abs(covered - gross) <= gross * 1e-10
                        and not result['positions']['conflicting_instrument_listings']
                        and not result['positions']['duplicate_account_position_extras'])
        hhi = sum(w * w for w in weights) if complete else None
        overlaps = []
        for i, first in enumerate(by_account.values()):
            for second in list(by_account.values())[i + 1:]:
                if complete and sum(first.values()) and sum(second.values()):
                    overlaps.append(100 * sum(min(first.get(key, 0) / sum(first.values()),
                                                  second.get(key, 0) / sum(second.values()))
                                              for key in first.keys() | second.keys()))
        result['exposure_math'] = {
            'basis': 'Known gross absolute saved security values; excludes cash and unavailable holdings; no fund look-through',
            'identity_value_coverage_pct': round(covered / gross * 100, 4) if gross else None,
            'hhi_fraction': round(hhi, 6) if hhi else None,
            'effective_equal_weight_instruments': round(1 / hhi, 4) if hhi else None,
            'largest_10pct_price_fall_gross_exposure_impact_pct': round(-10 * weights[0], 4) if complete and not any(r['value'] < 0 for r in rows if valid(r['value'])) else None,
            'top5_10pct_price_fall_gross_exposure_impact_pct': round(-10 * sum(weights[:5]), 4) if complete and not any(r['value'] < 0 for r in rows if valid(r['value'])) else None,
            'shock_assumptions': 'Only named positions fall 10%; other prices, units, and FX stay fixed. Static scenario, not a forecast.',
            'account_pair_security_overlap_pct': investment_profile.quantiles(overlaps),
            'overlap_formula': '100 * sum(min(account A normalized gross weight, account B normalized gross weight)) by exact instrument ID',
        }
        currency_values = defaultdict(float)
        for row in rows:
            if valid(row['value']):
                currency_values[row.get('market_currency') or 'Unknown'] += abs(row['value'])
        result['quote_currency_exposure_pct'] = {code: round(value / gross * 100, 4)
                                                for code, value in sorted(currency_values.items())} if gross else {}
        result['position_arithmetic'] = {
            'quantity_times_price_comparable': sum(all(valid(p.get(k)) for k in ('quantity', 'price', 'value')) for p in raw),
            'quantity_times_price_mismatch_above_1pct': sum(
                abs(p['quantity'] * p['price'] * (p.get('multiplier') or 1) - p['value']) > max(.01, abs(p['value']) * .01)
                for p in raw if all(valid(p.get(k)) for k in ('quantity', 'price', 'value'))),
            'position_vs_listing_currency_different': sum(p.get('currency') != p.get('listing_currency') for p in raw),
            'within_account_repeated_instrument_groups': sum(n > 1 for n in Counter(
                (a['id'], p['instrument_id']) for a in accounts for p in a['positions']['data']
                if not p.get('is_cash') and p.get('instrument_id')).values()),
        }
        incomplete = Counter()
        for period in ('1w', '1m', '3m', '6m', 'ytd', '1y'):
            for position in raw:
                market = investing_analytics._market(position, markets, today)
                points = market['points']
                cutoff = investing_analytics._cutoff(period, today)
                if points and points[-1]['date'] > cutoff.isoformat() and investing_analytics._anchor(points, cutoff) is None:
                    incomplete[period] += 1
        result['requested_period_missing_baselines'] = dict(sorted(incomplete.items()))
        activities = [r for a in accounts for r in a['activities']['data']]
        dates = [r['date'][:10] for r in activities if r.get('date')]
        result['activity'].update(first_date=min(dates) if dates else None, last_date=max(dates) if dates else None,
                                  funding_rows=sum(r.get('type') in {'DEPOSIT', 'WITHDRAWAL', 'TRANSFER'} for r in activities),
                                  personal_return_readiness='Not established: cached activities do not prove complete dated external cash flows')
        connections = cached.get('connections', {}).get('data', [])
        result['connections'] = {'count': len(connections), 'disabled': sum(bool(c.get('disabled')) for c in connections),
                                 'unmatched_account_connection_ids': sum(a.get('connection_id') not in {c['id'] for c in connections} for a in accounts),
                                 'cache_saved_at': cached.get('cache', {}).get('saved_at'),
                                 'cache_last_successful_refresh_at': cached.get('cache', {}).get('last_successful_refresh_at'),
                                 'cache_stale': cached.get('cache', {}).get('stale')}
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {}
        for table in ('snaptrade_snapshots', 'snaptrade_account_observations', 'snaptrade_position_observations',
                      'snaptrade_account_estimates', 'investment_market_cache', 'investment_market_closes',
                      'investment_intraday_cache', 'investment_intraday_bars', 'wealth_source_links'):
            counts[table] = conn.execute('SELECT count(*) FROM ' + table).fetchone()[0] if table in tables else None
        result['storage'] = {'row_counts': counts, 'integrity': conn.execute('PRAGMA integrity_check').fetchone()[0],
                             'foreign_key_violations': len(conn.execute('PRAGMA foreign_key_check').fetchall())}
        if {'snaptrade_position_observations', 'snaptrade_account_observations'} <= tables:
            result['storage']['positions_without_snapshot_account'] = conn.execute('''
                SELECT count(*) FROM snaptrade_position_observations p
                LEFT JOIN snaptrade_account_observations a
                  ON a.snapshot_id=p.snapshot_id AND a.account_id=p.account_id
                WHERE a.account_id IS NULL''').fetchone()[0]
        if {'investment_market_closes', 'investment_market_cache'} <= tables:
            result['storage']['daily_closes_without_market_identity'] = conn.execute('''
                SELECT count(*) FROM investment_market_closes p
                LEFT JOIN investment_market_cache m ON m.instrument_id=p.instrument_id
                WHERE m.instrument_id IS NULL''').fetchone()[0]
        result['method'] = {'network_calls': 0, 'database_writes': 0, 'query_only': True,
                            'source': 'Supplied read-only SQLite database; snapshot provenance is recorded in the accompanying review',
                            'formulas': {'HHI': 'sum((instrument gross value / total known gross security value)^2)',
                                         'effective_instruments': '1 / HHI'}}
        return result
    finally:
        conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--as-of', type=date.fromisoformat, default=datetime.now(timezone.utc).date())
    args = parser.parse_args()
    print(json.dumps(analyze(args.db, args.as_of), indent=2, sort_keys=True, allow_nan=False))

"""Read-only history audit. Emits coverage and timings, never account identities or money.

python backend/scripts/audit_investment_history.py --base-url http://127.0.0.1:8080
Reads only the local wealth projection; does not refresh providers or load secrets.
"""
import argparse
from collections import Counter
import ipaddress
import json
import statistics
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, build_opener

from benchmark_investments import NoRedirects


def audit(base):
    parsed = urlsplit(base)
    try:
        private = parsed.hostname == 'localhost' or ipaddress.ip_address(parsed.hostname).is_private
    except ValueError:
        private = False
    if not private or parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/'}:
        raise ValueError('Choose a private LAN or local origin without credentials.')
    opener = build_opener(ProxyHandler({}), NoRedirects())

    def get(query):
        started = time.perf_counter()
        with opener.open(base.rstrip('/') + '/api/wealth?' + urlencode(query, doseq=True), timeout=30) as response:
            payload = json.load(response)
            status = response.headers.get('X-Ledger-Investment-Cache')
        return payload, round((time.perf_counter() - started) * 1000, 2), status

    initial, _, _ = get({'range': '1m'})
    selected = set(initial['selected_account_ids'])
    investments = [row['id'] for row in initial['accounts'] if row['id'] in selected and row['kind'] == 'investment']
    results = []
    for scope, accounts in [('net_assets', None), ('investments', investments)]:
        for window in ['1w', '1m', '1y', 'all']:
            query = {'range': window}
            if accounts is not None:
                query['account'] = accounts or ['__none__']
            samples = [get(query) for _ in range(3)]
            data = samples[-1][0]
            history = data['history']
            points = history['points']
            results.append({'scope': scope, 'range': window,
                'selected_accounts': history['selected_accounts'], 'history_accounts': history['account_count'],
                'start_date': history['start_date'], 'end_date': history['end_date'],
                'dated_points': len(points), 'valued_points': sum(p['net_worth'] is not None for p in points),
                'reconstructed_points': sum(p.get('reconstructed_accounts', 0) > 0 for p in points),
                'valuation_bases': sorted({p['valuation_basis'] for p in points}),
                'provider_conflict_accounts': sum(bool(a.get('history_provider_conflict')) for a in data['accounts']),
                'provider_discontinuity_accounts': sum(bool(a.get('history_discontinuities')) for a in data['accounts']),
                'change_comparable': history['change_comparable'],
                'first_request_ms': samples[0][1],
                'repeated_median_ms': round(statistics.median(s[1] for s in samples[1:]), 2),
                'cache_status_counts': dict(Counter(s[2] for s in samples))})
    return {'generated_at': initial['generated_at'], 'provider_refreshes': 0, 'results': results}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8080')
    args = parser.parse_args()
    try:
        print(json.dumps(audit(args.base_url), indent=2))
    except Exception:
        raise SystemExit('History audit failed; no account-specific response is printed.') from None

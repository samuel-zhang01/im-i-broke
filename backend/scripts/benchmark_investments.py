"""Read-only LAN benchmark; emits timings/counts, never holdings or identities.

Example: python backend/scripts/benchmark_investments.py \
    --base-url http://127.0.0.1:8080 --output output/investment-performance.json
No provider refresh, authentication mutation, credentials, or raw body logging.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import ipaddress
import json
import math
from pathlib import Path
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def benchmark(base, iterations=12, *, include_insights=False):
    parsed = urllib.parse.urlsplit(base)
    try:
        private = parsed.hostname == 'localhost' or ipaddress.ip_address(parsed.hostname).is_private
    except ValueError:
        private = False
    if parsed.scheme not in {'http', 'https'} or not private or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/'}:
        raise ValueError('Choose a local or private LAN HTTP(S) origin without credentials.')
    if not 2 <= iterations <= 100:
        raise ValueError('Choose between 2 and 100 requests per endpoint.')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects())

    def read(path):
        with opener.open(base.rstrip('/') + path, timeout=30) as response:
            return response.read(), response.headers

    # /connected can populate an empty cache through the provider. Select a
    # security from a saved snapshot instead so this tool only reads local data,
    # even if the current cache was cleared while observations remain.
    snapshots = json.loads(read('/api/investments/connected/snapshots')[0]).get('snapshots', [])
    if not snapshots:
        raise ValueError('No saved investment snapshot is available.')
    snapshot_id = int(snapshots[0]['id'])
    if snapshot_id < 1:
        raise ValueError('No valid saved investment snapshot is available.')
    portfolio = json.loads(read(f'/api/investments/connected/snapshots/{snapshot_id}')[0])
    position = next(((account, holding) for account in portfolio.get('accounts', [])
                     for holding in account.get('positions', {}).get('data', [])), None)
    checks = [('analysis', '/api/investments/analysis?range=1m')]
    if position:
        account, holding = position
        checks.append(('security', '/api/investments/analysis/security?' + urllib.parse.urlencode({
            'account': account['id'], 'position': holding['id'], 'range': '1m'})))
    checks.extend([('calendar', '/api/investments/analysis/calendar?source=estimate'),
                   ('benchmarks', '/api/investments/market/benchmarks?range=1m')])
    if include_insights:
        checks.append(('insights', '/api/investments/analysis/insights'))
    results = []
    for name, path in checks:
        times, sizes, statuses, processing = [], [], Counter(), []
        for _ in range(iterations):
            started = time.perf_counter()
            body, headers = read(path)
            times.append((time.perf_counter() - started) * 1000)
            sizes.append(len(body))
            status = headers.get('X-Ledger-Investment-Cache', 'unreported')
            statuses[status if status in {'HIT', 'MISS', 'BYPASS', 'COALESCED'} else 'unreported'] += 1
            for timing in headers.get('Server-Timing', '').split(','):
                if timing.strip().startswith('investment_read;dur='):
                    try:
                        processing.append(float(timing.split('dur=')[1].split(';')[0]))
                    except ValueError:
                        pass
        warm = times[1:]
        results.append({'endpoint': name, 'requests': iterations,
            'first_ms': round(times[0], 2), 'median_ms': round(statistics.median(warm), 2),
            'p95_ms': round(sorted(warm)[math.ceil(len(warm) * .95) - 1], 2),
            'response_bytes': max(sizes), 'cache_status_counts': dict(statuses),
            'server_processing_median_ms': round(statistics.median(processing), 2) if processing else None})
    return {'measured_at': datetime.now(timezone.utc).isoformat(),
            'method': 'Sequential GETs; first request separated, median/p95 use remaining requests. Wall time includes local HTTP and concurrent machine load.',
            'provider_refreshes': 0, 'results': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:200')
    parser.add_argument('--iterations', type=int, default=12)
    parser.add_argument('--include-insights', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        result = benchmark(args.base_url, args.iterations, include_insights=args.include_insights)
    except Exception as error:
        # URL/HTTP exceptions can contain account-specific paths. Never print
        # them or a traceback from this intentionally aggregate-only tool.
        print(json.dumps({'error': 'Read-only investment benchmark failed.',
                          'http_status': error.code if isinstance(error, urllib.error.HTTPError) else None}))
        raise SystemExit(1) from None
    content = json.dumps(result, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content)
    print(content, end='')


if __name__ == '__main__':
    main()

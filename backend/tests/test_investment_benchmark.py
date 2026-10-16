import io
import json
import unittest
from unittest.mock import patch

from scripts import benchmark_investments as benchmark


class LocalResponses:
    def __init__(self, snapshots=True):
        self.paths = []
        self.snapshots = snapshots

    def open(self, url, timeout):
        path = url.removeprefix('http://127.0.0.1:200')
        self.paths.append(path)
        if path == '/api/investments/connected/snapshots':
            payload = {'snapshots': [{'id': 1}] if self.snapshots else []}
        elif path == '/api/investments/connected/snapshots/1':
            payload = {'accounts': [{'id': 'private-account-fixture', 'positions': {'data': [
                {'id': 'private-position-fixture'}]}}]}
        elif path.startswith('/api/investments/analysis') or path.startswith('/api/investments/market/benchmarks'):
            payload = {'sensitive_value': 'never-copy-this-body'}
        else:
            raise AssertionError('Only saved local data may be read.')
        response = io.BytesIO(json.dumps(payload).encode())
        response.headers = {'X-Ledger-Investment-Cache': 'HIT',
                            'Server-Timing': 'investment_read;dur=0.31, investment_cache;desc="HIT"'}
        return response


class InvestmentBenchmarkTests(unittest.TestCase):
    def test_empty_snapshot_list_stops_before_any_potential_provider_read(self):
        responses = LocalResponses(snapshots=False)
        with patch.object(benchmark.urllib.request, 'build_opener', return_value=responses):
            with self.assertRaises(ValueError):
                benchmark.benchmark('http://127.0.0.1:200', 2)
        self.assertEqual(responses.paths, ['/api/investments/connected/snapshots'])

    def test_saved_snapshot_selection_and_receipt_emit_aggregate_timings_only(self):
        responses = LocalResponses()
        with patch.object(benchmark.urllib.request, 'build_opener', return_value=responses):
            receipt = benchmark.benchmark('http://127.0.0.1:200', 2, include_insights=True)
        self.assertEqual(len(receipt['results']), 5)
        self.assertEqual(receipt['provider_refreshes'], 0)
        self.assertNotIn('/api/investments/connected', responses.paths)
        rendered = json.dumps(receipt)
        for private in ('private-account-fixture', 'private-position-fixture', 'never-copy-this-body'):
            self.assertNotIn(private, rendered)
        self.assertTrue(all(row['requests'] == 2 and row['cache_status_counts'] == {'HIT': 2}
                            for row in receipt['results']))

    def test_public_origins_and_url_credentials_are_rejected_before_network_access(self):
        with patch.object(benchmark.urllib.request, 'build_opener') as opener:
            for origin in ('https://example.com', 'http://user:password@127.0.0.1:200',
                           'http://127.0.0.1:200/?token=secret'):
                with self.assertRaises(ValueError):
                    benchmark.benchmark(origin, 2)
            opener.assert_not_called()

"""Bank monitoring must remain local, bounded, read-only and aggregate-only."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app import banking_health as health, lunchflow_store as store, lunchflow_client as client
from app.db import init_db


class BankingHealthTests(unittest.TestCase):
    def test_no_cache_reports_missing_schema_without_creating_it(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.execute('PRAGMA query_only=ON')
        result = health.snapshot(conn)
        self.assertFalse(result['schema_ready'])
        self.assertTrue(result['read_only'])
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM sqlite_master').fetchone()[0], 0)

    def test_snapshot_retains_counts_and_freshness_without_financial_payload_or_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = init_db(Path(folder) / 'bank.db')
            self.addCleanup(conn.close)
            store.ensure_schema(conn)
            marker = 'private-value-never-in-health'
            accounts = client.list_accounts({'accounts': [{
                'id': marker, 'name': marker, 'institution_name': marker,
                'status': 'ACTIVE', 'private': marker}]})
            store.discover_accounts(conn, accounts, '2026-09-15T10:00:00+00:00')
            raw = {'id': 'native', 'accountId': marker, 'date': '2026-09-15', 'amount': -12,
                   'currency': 'SGD', 'merchant': marker, 'description': marker, 'isPending': False}
            pending = {**raw, 'id': 'pending', 'currency': 'GBP', 'isPending': True}
            rows = client.transactions({'transactions': [raw, pending]}, marker)
            store.record_account_result(conn, marker, {'balance': {'amount': 100, 'currency': 'GBP'},
                                        'transactions': rows, 'errors': []}, '2026-09-15T10:01:00+00:00')
            conn.execute('UPDATE bank_connections SET last_error=?', (marker,))
            conn.commit()
            before = list(conn.iterdump())
            conn.execute('PRAGMA query_only=ON')
            result = health.snapshot(conn, datetime(2026, 9, 18, 10, tzinfo=timezone.utc))
            self.assertEqual(result['cache']['total'], 2)
            self.assertEqual(result['review_queue']['pending_authorizations'], 1)
            self.assertEqual(result['review_queue']['native_currency_records'], 1)
            self.assertEqual(result['connections']['stale_balance'], 1)
            self.assertEqual(result['connections']['failed'], 1)
            self.assertGreater(result['freshness']['oldest_complete_account_sync_age_seconds'], 36 * 3600)
            self.assertEqual(sum(result['integrity_issues'].values()), 0)
            self.assertNotIn(marker, json.dumps(result))
            for private_key in ('raw', 'amount', 'merchant', 'description', 'account_id', 'last_error'):
                self.assertNotIn('"' + private_key + '"', json.dumps(result))
            self.assertEqual(before, list(conn.iterdump()))

    def test_request_timing_keeps_only_fixed_labels_and_bounded_numeric_samples(self):
        with patch.object(health, '_timings', {}):
            for duration in range(300):
                self.assertTrue(health.record_request('summary', 200, duration))
            self.assertTrue(health.record_request('transactions', 422, 2))
            self.assertTrue(health.record_request('sync', 503, 3))
            for operation, status, duration in [
                    ('/api/banking/accounts/private-id', 200, 1),
                    ('summary', True, 1), ('summary', 200, float('nan')),
                    ('summary', 200, float('inf')), ('summary', 200, -1),
                    ('summary', 200, 'private-text'), ('summary', 200, 10 ** 20)]:
                self.assertFalse(health.record_request(operation, status, duration))
            snapshot = health.timing_snapshot()
            record = snapshot['operations']['summary']
            self.assertEqual(record['requests'], 300)
            self.assertEqual(record['recent_samples'], 128)
            self.assertEqual(record['max_ms'], 299)
            self.assertGreaterEqual(record['recent_p95_ms'], record['recent_p50_ms'])
            self.assertEqual(snapshot['operations']['transactions']['client_errors'], 1)
            self.assertEqual(snapshot['operations']['sync']['server_errors'], 1)
            self.assertNotIn('private', json.dumps(snapshot))


if __name__ == '__main__':
    unittest.main()

"""Failure/recovery checks for account identity, currencies and cache history."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.db import init_db
from app import lunchflow_client as client, lunchflow_store as store


class LunchFlowHealthTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'bank.db'
        self.conn = init_db(self.path)
        self.addCleanup(self.conn.close)
        store.ensure_schema(self.conn)

    def discover(self, *accounts):
        payload = [{'id': key, 'name': 'Current account', 'institution_name': 'Example Bank',
                    'status': status} for key, status in accounts]
        store.discover_accounts(self.conn, client.list_accounts({'accounts': payload}),
                                '2026-09-18T10:00:00+00:00')

    def save(self, key, *, balance_currency='GBP', transaction_currency='GBP',
             stamp='2026-09-18T10:01:00+00:00', description='Synthetic purchase'):
        raw = {'id': 'tx', 'accountId': key, 'date': '2026-09-17', 'amount': -7,
               'currency': transaction_currency, 'description': description, 'isPending': False}
        result = {'balance': {'amount': 100, 'currency': balance_currency} if balance_currency else None,
                  'transactions': client.transactions({'transactions': [raw]}, str(key)),
                  'errors': [] if balance_currency else ['Synthetic balance unavailable']}
        store.record_account_result(self.conn, str(key), result, stamp)

    def test_transaction_currency_cannot_create_account_when_balance_read_fails(self):
        self.discover((42, 'ACTIVE'))
        self.save(42, balance_currency=None, transaction_currency='SGD')
        account = self.conn.execute('SELECT * FROM bank_connections').fetchone()
        self.assertIsNone(account['currency'])
        self.assertIsNone(account['account_id'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 0)
        cached = self.conn.execute('SELECT currency,status,raw FROM bank_transactions').fetchone()
        self.assertEqual(cached['currency'], 'SGD')
        self.assertEqual(cached['status'], 'waiting_mapping')
        self.assertEqual(json.loads(cached['raw'])['currency'], 'SGD')

        self.save(42, balance_currency='GBP', transaction_currency='SGD',
                  stamp='2026-09-18T11:01:00+00:00')
        account = self.conn.execute('SELECT * FROM bank_connections').fetchone()
        self.assertEqual(account['currency'], 'GBP')
        self.assertIsNotNone(account['account_id'])
        self.assertIsNone(account['last_error'])
        self.assertEqual(self.conn.execute('SELECT status FROM bank_transactions').fetchone()[0], 'native')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 0)

    def test_reconnecting_provider_id_cannot_silently_duplicate_ledger_history(self):
        self.discover((42, 'ACTIVE'))
        self.save(42)
        original = [tuple(row) for row in self.conn.execute('SELECT * FROM transactions')]
        self.discover((43, 'ACTIVE'))
        self.save(43)
        account = self.conn.execute("SELECT * FROM bank_connections WHERE id='43'").fetchone()
        self.assertIsNone(account['account_id'])
        self.assertEqual([tuple(row) for row in self.conn.execute('SELECT * FROM transactions')], original)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM accounts').fetchone()[0], 1)
        view = store.account_view(self.conn, account)
        self.assertEqual(view['mapping_status'], 'review')
        self.assertIn('previous', view['mapping_reason'].lower())

    def test_changed_account_list_currency_preserves_mapping_balance_and_history(self):
        self.discover((42, 'ACTIVE'))
        self.save(42)
        original_ledger = [tuple(row) for row in self.conn.execute('SELECT * FROM transactions')]
        original_balances = [tuple(row) for row in self.conn.execute('SELECT * FROM bank_balance_observations')]
        original_link = self.conn.execute('SELECT transaction_id FROM bank_transactions').fetchone()[0]
        original_sync = self.conn.execute('SELECT last_synced_at FROM bank_connections').fetchone()[0]
        payload = {'accounts': [{'id': 42, 'name': 'Current account', 'institution_name': 'Example Bank',
                                 'status': 'ACTIVE', 'currency': 'SGD'}]}
        store.discover_accounts(self.conn, client.list_accounts(payload), '2026-09-18T11:00:00+00:00')
        self.assertIn('currency', self.conn.execute('SELECT last_error FROM bank_connections').fetchone()[0])
        # Include both the existing linked row and an unseen GBP transaction:
        # neither a changed balance nor apparently usable GBP data may relabel
        # or extend this ledger while the account identity is in conflict.
        existing = json.loads(self.conn.execute('SELECT raw FROM bank_transactions').fetchone()[0])
        incoming = [existing, {**existing, 'id': 'new-gbp', 'date': '2026-09-18'},
                    {**existing, 'id': 'new-sgd', 'currency': 'SGD'}]
        result = {'balance': {'amount': 200, 'currency': 'SGD'},
                  'transactions': client.transactions({'transactions': incoming}, '42'), 'errors': []}
        store.record_account_result(self.conn, '42', result, '2026-09-18T11:01:00+00:00')
        account = self.conn.execute('SELECT * FROM bank_connections').fetchone()
        self.assertEqual(account['currency'], 'GBP')
        self.assertEqual(self.conn.execute('SELECT currency FROM accounts').fetchone()[0], 'GBP')
        self.assertEqual(account['last_synced_at'], original_sync)
        self.assertIn('currency', account['last_error'])
        self.assertEqual([tuple(row) for row in self.conn.execute('SELECT * FROM transactions')], original_ledger)
        self.assertEqual([tuple(row) for row in self.conn.execute('SELECT * FROM bank_balance_observations')], original_balances)
        self.assertEqual(self.conn.execute("SELECT transaction_id FROM bank_transactions WHERE provider_id='tx'").fetchone()[0], original_link)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM bank_transactions WHERE transaction_id IS NULL AND status='review'").fetchone()[0], 2)

        # A consistent subsequent account response recovers without requiring a
        # permanent error flag or losing the observations saved during conflict.
        payload['accounts'][0]['currency'] = 'GBP'
        store.discover_accounts(self.conn, client.list_accounts(payload), '2026-09-18T12:00:00+00:00')
        self.save(42, stamp='2026-09-18T12:01:00+00:00')
        self.assertIsNone(self.conn.execute('SELECT last_error FROM bank_connections').fetchone()[0])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM bank_transactions').fetchone()[0], 3)
        self.assertEqual(self.conn.execute("SELECT status FROM bank_transactions WHERE provider_id='new-sgd'").fetchone()[0], 'native')

    def test_list_only_or_balance_only_currency_conflict_cannot_import_new_history(self):
        for list_currency,balance_currency in [('SGD','GBP'), ('GBP','SGD')]:
            with self.subTest(list_currency=list_currency,balance_currency=balance_currency):
                folder = tempfile.TemporaryDirectory()
                self.addCleanup(folder.cleanup)
                conn = init_db(Path(folder.name) / 'bank.db')
                self.addCleanup(conn.close)
                store.ensure_schema(conn)
                payload = {'accounts': [{'id': 42, 'name': 'Current account', 'institution_name': 'Example Bank',
                                         'status': 'ACTIVE', 'currency': 'GBP'}]}
                store.discover_accounts(conn, client.list_accounts(payload), '2026-09-18T10:00:00+00:00')
                store.record_account_result(conn, '42', {'balance': {'amount': 100, 'currency': 'GBP'},
                    'transactions': [], 'errors': []}, '2026-09-18T10:01:00+00:00')
                payload['accounts'][0]['currency'] = list_currency
                store.discover_accounts(conn, client.list_accounts(payload), '2026-09-18T11:00:00+00:00')
                raw = {'id': 'new', 'accountId': 42, 'date': '2026-09-18', 'amount': -7,
                       'currency': 'GBP', 'isPending': False, 'description': 'Synthetic purchase'}
                store.record_account_result(conn, '42', {'balance': {'amount': 200, 'currency': balance_currency},
                    'transactions': client.transactions({'transactions': [raw]}, '42'), 'errors': []},
                    '2026-09-18T11:01:00+00:00')
                self.assertEqual(conn.execute('SELECT currency FROM bank_connections').fetchone()[0], 'GBP')
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM bank_balance_observations').fetchone()[0], 1)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 0)
                self.assertEqual(conn.execute('SELECT status FROM bank_transactions').fetchone()[0], 'review')
                self.assertIn('currency', conn.execute('SELECT last_error FROM bank_connections').fetchone()[0])

    def test_inactive_connection_also_guards_reconnect_but_explicit_new_is_allowed(self):
        self.discover((42, 'ACTIVE'))
        self.save(42)
        self.discover((42, 'EXPIRED'), (43, 'ACTIVE'))
        self.save(43)
        self.assertIsNone(self.conn.execute("SELECT account_id FROM bank_connections WHERE id='43'").fetchone()[0])
        store.map_account(self.conn, '43', create_new=True, account_type='current', confirm_distinct_account=True)
        account = self.conn.execute("SELECT * FROM bank_connections WHERE id='43'").fetchone()
        self.assertIsNotNone(account['account_id'])
        self.assertIsNone(store.account_view(self.conn, account)['mapping_reason'])

    def test_only_changed_raw_observations_add_revision_history(self):
        self.discover((42, 'ACTIVE'))
        self.save(42)
        original = self.conn.execute('SELECT raw FROM bank_transactions').fetchone()[0]
        self.save(42, stamp='2026-09-18T11:01:00+00:00')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM bank_transaction_revisions').fetchone()[0], 0)
        self.save(42, stamp='2026-09-18T12:01:00+00:00', description='Corrected bank text')
        self.assertEqual(self.conn.execute('SELECT raw FROM bank_transaction_revisions').fetchone()[0], original)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 1)

    def test_opening_initialized_cache_does_not_require_a_write_transaction(self):
        self.conn.commit()
        self.conn.execute('PRAGMA query_only=ON')
        with patch.object(store, 'connect', return_value=self.conn):
            self.assertIs(store.database(self.path), self.conn)

    def test_missing_declared_index_is_restored_without_clearing_cached_rows(self):
        self.discover((42, 'ACTIVE'))
        self.save(42)
        original = [tuple(row) for row in self.conn.execute('SELECT * FROM bank_transactions')]
        self.conn.execute('DROP INDEX ix_bank_transaction_account')
        self.conn.commit()
        with patch.object(store, 'connect', return_value=self.conn):
            store.database(self.path)
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='ix_bank_transaction_account'").fetchone())
        self.assertEqual([tuple(row) for row in self.conn.execute('SELECT * FROM bank_transactions')], original)


if __name__ == '__main__':
    unittest.main()

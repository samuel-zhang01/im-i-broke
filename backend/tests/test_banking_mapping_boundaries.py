"""Mapping cannot reinterpret saved money or race another connection's choice."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import account_settings, db, lunchflow_client, lunchflow_routes, lunchflow_store, planning


class BankingMappingBoundaryTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(folder) / 'synthetic.db'
        self.conn = db.init_db(self.path)
        self.addCleanup(self.conn.close)
        with self.conn:
            self.account = account_settings.create_manual(self.conn, account_settings.ManualAccount(
                display_name='Synthetic current account', institution='Example Bank', account_type='current'))['id']
            feeds = lunchflow_client.list_accounts({'accounts': [
                {'id': identifier, 'name': 'Main', 'institution_name': 'Example Bank', 'currency': 'GBP'}
                for identifier in ('first', 'second')]})
            lunchflow_store.discover_accounts(self.conn, feeds, '2026-09-01T00:00:00+00:00')

    def test_mapping_cannot_flip_positive_statement_cash_into_card_debt(self):
        with self.conn:
            self.conn.execute('''INSERT INTO statements(account_id,source_file,period_start,period_end,closing_balance,reconciled)
                VALUES(?,'synthetic.pdf','2026-08-01','2026-08-31',100,1)''', (self.account,))
        app = FastAPI()
        app.include_router(lunchflow_routes.router)
        with patch.object(lunchflow_store, 'DB_PATH', self.path), TestClient(app) as client:
            response = client.post('/api/banking/accounts/first/mapping', json={
                'account_id': self.account, 'account_type': 'credit', 'balance_multiplier': -1})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn('financial history', response.json()['detail'])
        self.assertEqual(account_settings.account_view(self.conn, self.account)['account_type'], 'current')
        self.assertIsNone(self.conn.execute("SELECT account_id FROM bank_connections WHERE id='first'").fetchone()[0])
        self.assertEqual(planning.account_snapshot(self.conn, date(2026, 9, 1))[0]['balance'], 100)
        self.assertEqual(self.conn.execute('SELECT closing_balance FROM statements').fetchone()[0], 100)

    def test_empty_account_can_receive_reviewed_initial_credit_classification(self):
        with self.conn:
            lunchflow_store.map_account(self.conn, 'first', account_id=self.account,
                                        account_type='credit', balance_multiplier=-1)
        self.assertEqual(account_settings.account_view(self.conn, self.account)['account_type'], 'credit')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM account_settings_history').fetchone()[0], 2)

    def test_concurrent_mappings_admit_writer_before_checking_local_account(self):
        first_read, second_begin, release_first = Event(), Event(), Event()
        original = lunchflow_store.institution_matches
        def check(conn, account, institution):
            if conn.execute("SELECT account_id FROM bank_connections WHERE id='first'").fetchone()[0] is None:
                first_read.set()
                if not release_first.wait(5):
                    raise AssertionError('Concurrent mapping test did not release the first writer')
            return original(conn, account, institution)
        def map_connection(identifier):
            conn = db.connect(self.path)
            try:
                if identifier == 'second':
                    conn.set_trace_callback(lambda sql: second_begin.set() if sql == 'BEGIN IMMEDIATE' else None)
                with conn:
                    lunchflow_store.map_account(conn, identifier, account_id=self.account)
            finally:
                conn.close()
        with patch.object(lunchflow_store, 'institution_matches', side_effect=check), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(map_connection, 'first')
            try:
                self.assertTrue(first_read.wait(5))
                second = pool.submit(map_connection, 'second')
                self.assertTrue(second_begin.wait(5))
                self.assertFalse(second.done())
            finally:
                release_first.set()
            first.result(timeout=5)
            with self.assertRaisesRegex(ValueError, 'already linked'):
                second.result(timeout=5)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM bank_connections WHERE account_id=?', (self.account,)).fetchone()[0], 1)

    def test_concurrent_review_matches_report_conflict_without_corrupting_links(self):
        with self.conn:
            lunchflow_store.map_account(self.conn, 'first', account_id=self.account)
            target = self.conn.execute('''INSERT INTO transactions(account_id,txn_date,description,merchant,amount)
                VALUES(?,'2026-09-01','Synthetic coffee','Synthetic coffee',-9)''', (self.account,)).lastrowid
            cache_ids = [self.conn.execute('''INSERT INTO bank_transactions(connection_id,provider_id,txn_date,
                amount,currency,merchant,description,status,raw,first_seen_at,last_seen_at)
                VALUES('first',?,'2026-09-01',-9,'GBP','Synthetic coffee','Synthetic coffee','review','{}',
                '2026-09-02T00:00:00+00:00','2026-09-02T00:00:00+00:00')''', (identifier,)).lastrowid
                for identifier in ('first-row', 'second-row')]
        first_read, second_begin, release_first = Event(), Event(), Event()
        original = lunchflow_store.candidate_transactions
        def candidates(conn, account, row, **kwargs):
            if row['id'] == cache_ids[0]:
                first_read.set()
                if not release_first.wait(5):
                    raise AssertionError('Concurrent review test did not release the first writer')
            return original(conn, account, row, **kwargs)
        def review(cache_id):
            conn = db.connect(self.path)
            try:
                if cache_id == cache_ids[1]:
                    conn.set_trace_callback(lambda sql: second_begin.set() if sql == 'BEGIN IMMEDIATE' else None)
                with conn:
                    lunchflow_store.resolve_review(conn, cache_id, 'match', target)
            finally:
                conn.close()
        with patch.object(lunchflow_store, 'candidate_transactions', side_effect=candidates), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(review, cache_ids[0])
            try:
                self.assertTrue(first_read.wait(5))
                second = pool.submit(review, cache_ids[1])
                self.assertTrue(second_begin.wait(5))
                self.assertFalse(second.done())
            finally:
                release_first.set()
            first.result(timeout=5)
            with self.assertRaisesRegex(ValueError, 'already matched'):
                second.result(timeout=5)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM bank_transactions WHERE transaction_id=?', (target,)).fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM bank_review_decisions').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()

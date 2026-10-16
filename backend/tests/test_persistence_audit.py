"""File-backed persistence and concurrent-write regressions for real API paths."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import analytics, main, planning
from app.categorize import apply_categorization
from app.db import init_db
from app.ingest import _store_statement
from app.parsers.base import ParsedStatement, RawTxn


class PersistenceAuditTests(unittest.TestCase):
    def setUp(self):
        # Functional API fixtures use local mode; auth/security have dedicated suites.
        self.enterContext(patch.dict('os.environ', {'FINANCE_AUTH_ENABLED': 'false', 'FINANCE_DEPLOYMENT': 'local'}))
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'finance.db'
        self.source = Path(self.temp.name) / 'storage-fixture.txt'
        self.source.write_text('Independent storage fixture')
        self.statement = ParsedStatement('Audit bank', 'current', 'audit-bank', None, '1234',
            date(2026, 1, 1), date(2026, 1, 31), 0, 920,
            [RawTxn(date(2026, 1, 5), 'Audit cafe', -100, category='Shopping', raw='2026-01-05 Audit cafe -100.00'),
             RawTxn(date(2026, 1, 6), 'Employer salary', 1000, category='Income'),
             RawTxn(date(2026, 1, 7), 'Audit cafe refund', 20, category='Shopping')])
        self.conn = init_db(self.path)
        _store_statement(self.conn, self.statement, self.source)
        apply_categorization(self.conn)
        analytics.recompute_all(self.conn)
        self.txn_id = self.conn.execute('SELECT id FROM transactions WHERE amount=-100').fetchone()[0]
        self.original = self.core_rows()
        self.db_patch = patch.object(main, 'DB_PATH', self.path)
        self.db_patch.start()
        self.client = TestClient(main.app, base_url='http://127.0.0.1', raise_server_exceptions=False)

    def tearDown(self):
        self.client.close()
        self.db_patch.stop()
        self.conn.close()
        self.temp.cleanup()

    def core_rows(self):
        return [tuple(row) for row in self.conn.execute(
            'SELECT id,account_id,txn_date,amount,dedupe_key,raw FROM transactions ORDER BY id')]

    def test_review_rules_scenarios_preferences_survive_restart_and_reimport(self):
        review = {'category': 'Groceries', 'merchant': 'My cafe', 'note': 'Persistent audit note',
                  'exclude_from_analysis': False}
        self.assertEqual(self.client.post(f'/api/transactions/{self.txn_id}/review', json=review).status_code, 200)
        rule = self.client.post('/api/rules', json={'pattern': 'Audit cafe', 'category': 'Eating out', 'priority': 5})
        self.assertEqual(rule.status_code, 200)
        current = self.client.get(f'/api/transactions/{self.txn_id}/review').json()
        self.assertEqual(current['transaction']['category'], 'Groceries')
        saved = self.client.post('/api/scenarios', json={'name': 'Persistence check', 'parameters': {
            'years': 1, 'annual_return': 0, 'inflation': 0, 'initial_wealth': 1000,
            'monthly_contribution': 100, 'one_off_amount': 500, 'one_off_month': 12}})
        self.assertEqual(saved.status_code, 200)
        settings = {'query': {'account': 'audit-bank', 'date_from': '2026-01-01', 'exclude_outliers': True}}
        self.assertEqual(self.client.put('/api/preferences/spending', json=settings).status_code, 200)
        self.client.delete(f"/api/rules/{rule.json()['id']}")
        self.assertEqual(self.client.post('/api/recompute', json={}).status_code, 200)
        reimported = _store_statement(self.conn, self.statement, self.source)
        self.assertEqual(reimported['new_txns'], 0)
        apply_categorization(self.conn)
        analytics.recompute_all(self.conn)
        # New app lifespan and new database connections simulate a server restart.
        with TestClient(main.app, base_url='http://127.0.0.1') as restarted:
            current = restarted.get(f'/api/transactions/{self.txn_id}/review').json()
            self.assertEqual(current['transaction']['category'], 'Groceries')
            self.assertEqual(current['transaction']['merchant'], 'My cafe')
            self.assertEqual(current['review']['note'], 'Persistent audit note')
            self.assertEqual(restarted.get('/api/preferences/spending').json()['value'], settings)
            self.assertEqual(restarted.get('/api/scenarios').json()[0]['id'], saved.json()['id'])
            self.assertEqual(restarted.delete(f'/api/transactions/{self.txn_id}/review').status_code, 200)
            self.assertEqual(restarted.get(f'/api/transactions/{self.txn_id}/review').json()['transaction']['category'], 'Shopping')
            self.assertEqual(restarted.delete(f"/api/scenarios/{saved.json()['id']}").status_code, 200)
        self.assertEqual(self.core_rows(), self.original)
        self.assertEqual(self.conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(self.conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_nested_scenario_parameter_types_are_rejected_without_writes(self):
        invalid = [{'baseline_method': []}, {'baseline_method': {}}, {'baseline_method': None},
                   {'years': True}, {'annual_return': []}, {'initial_wealth': {}}, {'exclude_outliers': 'false'},
                   {'initial_wealth': 10 ** 400}, {'years': 10 ** 400}]
        for parameters in invalid:
            with self.subTest(parameters=parameters):
                response = self.client.post('/api/scenarios', json={'name': 'Invalid', 'parameters': parameters})
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.client.get('/api/scenarios').json(), [])

    def test_legacy_category_input_lengths_return_validation_errors(self):
        for field, value in [('merchant', 'm' * 121), ('note', 'n' * 2001)]:
            with self.subTest(field=field):
                response = self.client.post(f'/api/transactions/{self.txn_id}/category',
                                            json={'category': 'Groceries', field: value})
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.client.get(f'/api/transactions/{self.txn_id}/review').json()['review'], {})

    def test_concurrent_partial_reviews_preserve_unrelated_fields(self):
        barrier = threading.Barrier(2)

        class PausedCursor(sqlite3.Cursor):
            def fetchone(cursor):
                row = super().fetchone()
                if not cursor.connection.audit_paused:
                    cursor.connection.audit_paused = True
                    # Both stale reads complete before either write in the buggy
                    # implementation. A serialized writer safely times out here,
                    # commits, then lets the second writer see its saved fields.
                    try:
                        barrier.wait(timeout=.2)
                    except threading.BrokenBarrierError:
                        pass
                return row

        class PausedConnection(sqlite3.Connection):
            audit_paused = False

            def execute(connection, sql, parameters=()):
                if sql.startswith('SELECT * FROM overrides WHERE dedupe_key='):
                    return connection.cursor(factory=PausedCursor).execute(sql, parameters)
                return super().execute(sql, parameters)

        def connection():
            conn = sqlite3.connect(self.path, timeout=5, factory=PausedConnection)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            return conn

        with patch.object(main, 'get_conn', side_effect=connection), ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(main.save_transaction_review, self.txn_id, main.TransactionReview(**body))
                    for body in [{'note': 'Written concurrently'}, {'exclude_from_analysis': True}]]
            for job in jobs:
                job.result(timeout=10)
        review = self.client.get(f'/api/transactions/{self.txn_id}/review').json()['review']
        self.assertEqual(review['note'], 'Written concurrently')
        self.assertEqual(review['exclude_from_analysis'], 1)
        self.assertEqual(self.core_rows(), self.original)

    def test_scenario_save_returns_its_committed_result_during_concurrent_delete(self):
        database = self.path

        class DeletedAfterCommit(sqlite3.Connection):
            def commit(connection):
                super().commit()
                # A different writer is allowed to remove a saved record after
                # commit. That must not make the original save raise a 500.
                with closing(sqlite3.connect(database)) as other, other:
                    other.execute('DELETE FROM planning_scenarios')

        conn = sqlite3.connect(self.path, factory=DeletedAfterCommit)
        conn.row_factory = sqlite3.Row
        try:
            saved = planning.save_scenario(conn, 'Concurrent save', {'years': 1})
        finally:
            conn.close()
        self.assertEqual(saved['name'], 'Concurrent save')
        self.assertEqual(saved['parameters']['years'], 1)
        self.assertEqual(self.client.get('/api/scenarios').json(), [])

    def test_failed_merchant_response_assembly_rolls_back_rule_and_classification(self):
        before = [tuple(row) for row in self.conn.execute(
            'SELECT id,merchant,category,classification FROM transactions ORDER BY id')]
        # Response assembly still runs inside the saved-name transaction. A
        # failure there must not leave a saved rule behind after reporting 500.
        with patch.object(main.merchants, 'list_merchants', side_effect=RuntimeError('Assembly failed')):
            response = self.client.post('/api/merchants', json={
                'canonical_name': 'Atomic cafe', 'category': 'Eating out',
                'aliases': [{'pattern': 'Audit cafe', 'match_mode': 'exact', 'context': 'spending'}]})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM merchants WHERE canonical_name='Atomic cafe'").fetchone()[0], 0)
        self.assertEqual([tuple(row) for row in self.conn.execute(
            'SELECT id,merchant,category,classification FROM transactions ORDER BY id')], before)

    def test_merchant_save_response_survives_concurrent_delete_after_commit(self):
        database = self.path

        class DeletedAfterCommit(sqlite3.Connection):
            def commit(connection):
                super().commit()
                with closing(sqlite3.connect(database)) as other, other:
                    other.execute('PRAGMA foreign_keys=ON')
                    other.execute("DELETE FROM merchants WHERE canonical_name='Atomic cafe'")

        conn = sqlite3.connect(self.path, factory=DeletedAfterCommit)
        conn.row_factory = sqlite3.Row
        try:
            saved = main._save_merchant(conn, main.MerchantUpdate(
                canonical_name='Atomic cafe', category='Eating out',
                aliases=[main.MerchantAlias(pattern='Audit cafe', match_mode='exact', context='spending')]))
        finally:
            conn.close()
        self.assertEqual(saved['merchant']['canonical_name'], 'Atomic cafe')
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM merchants WHERE canonical_name='Atomic cafe'").fetchone()[0], 0)

    def review_state(self):
        return {table: [tuple(row) for row in self.conn.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in ('transactions', 'overrides', 'anomaly_reviews', 'anomaly_review_exceptions', 'recurring')}

    def test_failed_review_save_response_rolls_back_classification_and_markers(self):
        before = self.review_state()
        with patch.object(main, '_review_result', side_effect=RuntimeError('Assembly failed')):
            response = self.client.post(f'/api/transactions/{self.txn_id}/review', json={
                'category': 'Groceries', 'note': 'Must roll back', 'exclude_from_analysis': True})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.review_state(), before)

    def test_failed_resolution_response_rolls_back_decision_and_queue_markers(self):
        ident = self.conn.execute("INSERT INTO transactions(account_id,txn_date,description,merchant,source_merchant,amount,category,dedupe_key) SELECT account_id,'2026-01-06',description,merchant,source_merchant,amount,category,'duplicate-cafe' FROM transactions WHERE id=?", (self.txn_id,)).lastrowid
        analytics.recompute_all(self.conn)
        self.assertTrue(self.conn.execute('SELECT is_anomaly FROM transactions WHERE id=?', (ident,)).fetchone()[0])
        before = self.review_state()
        with patch.object(main, '_review_result', side_effect=RuntimeError('Assembly failed')):
            response = self.client.post(f'/api/transactions/{ident}/review', json={'resolution': 'expected'})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.review_state(), before)

    def test_failed_review_reset_response_preserves_saved_review(self):
        url = f'/api/transactions/{self.txn_id}/review'
        self.assertEqual(self.client.post(url, json={'category': 'Groceries', 'note': 'Keep this review'}).status_code, 200)
        before = self.review_state()
        with patch.object(main, '_review_result', side_effect=RuntimeError('Assembly failed')):
            response = self.client.delete(url)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.review_state(), before)

    def test_review_save_response_describes_own_commit_during_concurrent_edit(self):
        database = self.path

        class EditedAfterCommit(sqlite3.Connection):
            def commit(connection):
                super().commit()
                with closing(sqlite3.connect(database)) as other, other:
                    other.execute("UPDATE overrides SET note='Concurrent edit'")

        conn = sqlite3.connect(self.path, factory=EditedAfterCommit)
        conn.row_factory = sqlite3.Row
        with patch.object(main, 'get_conn', return_value=conn):
            result = main.save_transaction_review(self.txn_id, main.TransactionReview(note='My saved note'))
        self.assertEqual(result['review']['note'], 'My saved note')
        self.assertEqual(self.client.get(f'/api/transactions/{self.txn_id}/review').json()['review']['note'], 'Concurrent edit')

"""Real preferences/wealth boundaries and independent SQLite writers, synthetic only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import db, main, wealth, workspace


class PreferenceWealthBoundaryTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(folder) / 'synthetic.db'
        self.enterContext(patch.object(db, 'DB_PATH', self.path))
        self.enterContext(patch.object(main, 'DB_PATH', self.path))
        self.enterContext(patch.dict('os.environ', {'FINANCE_AUTH_ENABLED': 'false', 'FINANCE_DEPLOYMENT': 'local'}))
        self.conn = db.init_db(self.path)
        self.addCleanup(self.conn.close)
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        self.today = date.today().isoformat()
        response = self.client.post('/api/workspace/accounts', json={'display_name': 'Synthetic savings', 'currency': 'GBP'})
        self.assertEqual(response.status_code, 201, response.text)
        self.account = response.json()['id']
        response = self.client.post(f'/api/workspace/accounts/{self.account}/balances', json={'amount': 100, 'currency': 'GBP', 'as_of': self.today})
        self.assertEqual(response.status_code, 201, response.text)

    def set_currency(self, code):
        response = self.client.patch('/api/workspace/preferences', json={'reporting_currency': code})
        self.assertEqual(response.status_code, 200, response.text)

    def test_absent_reporting_target_is_unknown_until_dated_rates_exist(self):
        self.set_currency('JPY')
        response = self.client.get('/api/wealth?currency=JPY&range=1m')
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data['currency'], 'JPY')
        self.assertEqual(data['currencies'], ['GBP', 'JPY'])
        self.assertIsNone(data['summary']['net_worth'])
        self.assertTrue(data['summary']['partial'])
        self.assertEqual(data['accounts'][0]['native_value'], 100)
        self.assertTrue(any('dated exchange rate' in warning for warning in data['warnings']))
        received = []
        def synthetic_rates(items):
            received.extend(items)
            return {(code, when): {'currency': code, 'date': when, 'observed_date': when,
                'rate_to_gbp': .005, 'source': 'Synthetic test rate', 'fetched_at': when} for code, when in items}
        wealth.refresh_fx(self.conn, path=self.path, fetch=synthetic_rates)
        self.assertIn(('JPY', self.today), received)
        self.assertEqual({code for code, _ in received}, {'JPY'})
        response = self.client.get('/api/wealth?currency=JPY&range=1m')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['summary']['net_worth'], 20000)
        self.assertEqual(response.json()['summary']['missing_accounts'], 0)
        self.assertFalse(response.json()['summary']['partial'])
        self.assertFalse(any('not reconciled' in warning for warning in response.json()['warnings']))
        self.assertEqual(self.client.get('/api/wealth?currency=GBP&range=1m').json()['summary']['net_worth'], 100)
        self.assertEqual(self.client.get('/api/wealth?currency=XYZ').status_code, 422)

    def test_manual_card_and_cash_keep_manual_provenance_without_statement_warning(self):
        created = self.client.post('/api/workspace/accounts', json={
            'display_name': 'Synthetic credit', 'currency': 'GBP', 'account_type': 'credit'})
        self.assertEqual(created.status_code, 201, created.text)
        account_id = created.json()['id']
        response = self.client.post(f'/api/workspace/accounts/{account_id}/balances', json={
            'amount': -250, 'currency': 'GBP', 'as_of': self.today})
        self.assertEqual(response.status_code, 201, response.text)
        response = self.client.get('/api/wealth?currency=GBP&range=1m')
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        card = next(account for account in data['accounts'] if account['id'] == f'statement:{account_id}')
        self.assertEqual(card['balance_source'], 'manual')
        self.assertEqual(card['valuation']['reported_source'], 'manual')
        self.assertEqual(card['value'], -250)
        self.assertFalse(card['valuation']['current_unknown'])
        self.assertEqual(data['summary']['net_worth'], -150)
        self.assertFalse(any('not reconciled' in warning for warning in data['warnings']))

    def test_reporting_target_is_not_dropped_by_account_currency_budget(self):
        self.set_currency('JPY')
        accounts = [{'observations': [{'currency': code, 'date': self.today}]} for code in
                    ['AED', 'AUD', 'CAD', 'CHF', 'CNY', 'DKK', 'EUR', 'HKD', 'USD']]
        received = []
        with patch.object(wealth, 'sources', return_value=(accounts, {})):
            wealth.refresh_fx(self.conn, path=self.path, fetch=lambda items: received.extend(items) or {})
        self.assertIn(('JPY', self.today), received)

    def test_disjoint_preference_updates_merge_after_writer_admission(self):
        first_read = threading.Event()
        second_begin = threading.Event()
        release_first = threading.Event()
        second_read = threading.Event()
        real_preferences = workspace.preferences

        def open_connection():
            conn = sqlite3.connect(self.path, timeout=5)
            conn.row_factory = sqlite3.Row
            if threading.current_thread().name.endswith('_1'):
                conn.set_trace_callback(lambda sql: second_begin.set() if sql == 'BEGIN IMMEDIATE' else None)
            return conn

        def read_preferences(conn):
            if threading.current_thread().name.endswith('_0'):
                result = real_preferences(conn)
                first_read.set()
                if not release_first.wait(5):
                    raise AssertionError('Concurrent preference test did not release first writer')
                return result
            second_read.set()
            return real_preferences(conn)

        with patch.object(workspace, '_open', side_effect=open_connection), patch.object(workspace, 'preferences', side_effect=read_preferences):
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix='preferences') as pool:
                first = pool.submit(workspace.update_preferences, workspace.PreferencesInput(locale='fr-FR'))
                try:
                    self.assertTrue(first_read.wait(5))
                    second = pool.submit(workspace.update_preferences, workspace.PreferencesInput(timezone='America/New_York'))
                    self.assertTrue(second_begin.wait(5))
                    self.assertFalse(second_read.is_set(), 'Second writer read stale JSON before writer admission')
                finally:
                    release_first.set()
                first.result(timeout=5)
                second.result(timeout=5)
        saved = real_preferences(self.conn)
        self.assertEqual(saved['locale'], 'fr-FR')
        self.assertEqual(saved['timezone'], 'America/New_York')
        history = self.conn.execute("SELECT before_json,after_json FROM workspace_changes WHERE kind='preferences' ORDER BY id").fetchall()
        self.assertEqual(len(history), 2)
        self.assertEqual(json.loads(history[1]['before_json']), json.loads(history[0]['after_json']))


if __name__ == '__main__':
    unittest.main()

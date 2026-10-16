"""Cross-screen workflow contracts, using disposable synthetic ledgers only."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main
from app.db import init_db


class WorkflowAuditTests(unittest.TestCase):
    def setUp(self):
        # Functional API fixtures use local mode; auth/security have dedicated suites.
        self.enterContext(patch.dict('os.environ', {'FINANCE_AUTH_ENABLED': 'false', 'FINANCE_DEPLOYMENT': 'local'}))
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'audit.db'
        self.conn = init_db(self.path)
        self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('a','Bank','current','Current'),('b','Bank','credit','Card')")
        self.conn.commit()
        self.path_patch = patch.object(main, 'DB_PATH', self.path)
        self.path_patch.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        self.path_patch.stop()
        self.conn.close()
        self.temp.cleanup()

    def payment(self, name, amount, category, day='2026-01-05', account='a', **extra):
        ident = self.conn.execute(
            'INSERT INTO transactions(account_id,txn_date,description,source_merchant,merchant,amount,category,dedupe_key) VALUES(?,?,?,?,?,?,?,?)',
            (account, day, name, name, name, amount, category, f'{name}:{day}:{amount}:{account}')).lastrowid
        for field, value in extra.items():
            self.assertIn(field, ('merchant_kind', 'spending_treatment', 'exclude_from_analysis'))
            self.conn.execute(f'UPDATE transactions SET {field}=? WHERE id=?', (value, ident))
        self.conn.commit()
        return ident

    def get(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def assert_spend(self, expected):
        observed = [
            -self.get('/api/summary')['spending'],
            sum(row['spent'] for row in self.get('/api/categories')),
            -sum(row['spending'] for row in self.get('/api/timeseries')['net']),
            self.get('/api/analytics')['stats']['total_spend'],
            sum(row['net'] for row in self.get('/api/spending-explorer')['cells']),
        ]
        for value in observed:
            self.assertAlmostEqual(value, expected, places=2)

    def seed_mixed_ledger(self):
        self.payment('Purchase', -100, 'Shopping', account='b')
        self.payment('Purchase refund', 25, 'Shopping', account='b')
        for day in ('2026-01-06', '2026-02-06', '2026-03-06'):
            self.payment('Stream', -10, 'Subscriptions', day=day)
        self.payment('Buddy dinner', -20, 'Friends & shared costs', merchant_kind='person')
        self.payment('Buddy gift', 8, 'Friends & shared costs', merchant_kind='person')
        self.payment('Salary', 1000, 'Income')
        self.payment('Own savings', -1000, 'Transfers & payments', spending_treatment='self_transfer')
        self.payment('Excluded', -700, 'Shopping', exclude_from_analysis=1)

    def test_spending_reconciles_across_screens_and_combined_filters(self):
        self.seed_mixed_ledger()
        self.assert_spend(125)
        self.assertEqual(self.get('/api/summary')['income'], 1008)
        for filters, expected in [
            ('account=b', 75),
            ('date_from=2026-02-01&date_to=2026-03-31', 20),
            ('account=b&date_from=2026-02-01&date_to=2026-03-31', 0),
            ('account=a&date_from=2026-02-01&date_to=2026-02-28&exclude_outliers=true', 10),
        ]:
            with self.subTest(filters=filters):
                analytics = self.get('/api/analytics?' + filters)
                explorer = self.get('/api/spending-explorer?' + filters)
                self.assertAlmostEqual(analytics['stats']['total_spend'], expected)
                self.assertAlmostEqual(sum(c['net'] for c in explorer['cells']), expected)

    def test_review_reset_restores_alias_pattern_and_preserves_source_evidence(self):
        ids = [self.payment('Processor Stream', -10, 'Uncategorized', day=day)
               for day in ('2026-01-06', '2026-02-06', '2026-03-06')]
        response = self.client.post('/api/merchants', json={
            'canonical_name': 'Stream', 'category': 'Subscriptions',
            'aliases': [{'pattern': 'Processor Stream', 'match_mode': 'exact', 'context': 'spending'}]})
        self.assertEqual(response.status_code, 200, response.text)
        original = tuple(self.conn.execute('SELECT description,source_merchant,amount,txn_date FROM transactions WHERE id=?', (ids[0],)).fetchone())
        self.assert_spend(30)
        url = f'/api/transactions/{ids[0]}/review'
        response = self.client.post(url, json={'exclude_from_analysis': True, 'note': 'Duplicate evidence'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_spend(20)
        self.assertEqual(self.get('/api/recurring?details=true')['patterns'], [])
        self.assertEqual(self.client.post('/api/recompute').status_code, 200)
        self.assertEqual(self.get(url)['review']['note'], 'Duplicate evidence')
        self.assertEqual(self.client.delete(url).status_code, 200)
        self.assert_spend(30)
        recurring = self.get('/api/recurring?details=true')
        self.assertEqual(set(recurring['patterns'][0]['txn_ids']), set(ids))
        self.assertEqual(self.get('/api/transactions?recurring=true')['total'], 3)
        self.assertEqual(self.get(url)['review'], {})
        after = tuple(self.conn.execute('SELECT description,source_merchant,amount,txn_date FROM transactions WHERE id=?', (ids[0],)).fetchone())
        self.assertEqual(after, original)

    def test_dashboard_reads_and_custom_previews_do_not_change_saved_ledger(self):
        self.seed_mixed_ledger()
        before = list(self.conn.iterdump())
        for url in [
            '/api/summary', '/api/transactions?limit=1&offset=1', '/api/timeseries',
            '/api/categories', '/api/recurring?details=true&min_occurrences=2',
            '/api/transfers?window_days=30', '/api/anomalies', '/api/advice',
            '/api/analytics?exclude_outliers=true', '/api/spending-explorer',
            '/api/planning?years=50&annual_return=-50&monthly_contribution=-1000',
            '/api/merchants', '/api/merchants/inbox', '/api/health', '/api/revision',
        ]:
            with self.subTest(url=url):
                self.get(url)
                self.assertEqual(list(self.conn.iterdump()), before)

    def test_rejected_saved_scenarios_do_not_persist_partial_changes(self):
        result = self.client.post('/api/scenarios', json={
            'name': 'Original', 'parameters': {'years': 2, 'monthly_contribution': 100}})
        self.assertEqual(result.status_code, 200, result.text)
        initial = self.get('/api/scenarios')
        ident = initial[0]['id']
        for parameters in ({'years': 1, 'one_off_month': 13}, {'years': 0},
                           {'annual_return': 'NaN'}, {'arbitrary': 1}):
            with self.subTest(parameters=parameters):
                response = self.client.post('/api/scenarios', json={
                    'id': ident, 'name': 'Invalid replacement', 'parameters': parameters})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(self.get('/api/scenarios'), initial)

    def test_pagination_aggregates_cover_filtered_set_and_never_duplicate_rows(self):
        self.seed_mixed_ledger()
        whole = self.get('/api/transactions?direction=out&sort=amount&order=asc')
        seen = []
        for offset in range(0, whole['total'], 2):
            page = self.get(f'/api/transactions?direction=out&sort=amount&order=asc&limit=2&offset={offset}')
            self.assertEqual(page['total'], whole['total'])
            self.assertEqual(page['sum_out'], whole['sum_out'])
            seen.extend(row['id'] for row in page['items'])
        self.assertEqual(seen, [row['id'] for row in whole['items']])
        self.assertEqual(len(seen), len(set(seen)))

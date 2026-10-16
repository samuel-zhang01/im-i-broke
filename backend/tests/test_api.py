import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from app import main
from app.db import init_db
from app.categorize import auto_category, apply_categorization


class ApiTests(unittest.TestCase):
    def setUp(self):
        # Functional API fixtures use local mode; auth/security have dedicated suites.
        self.enterContext(patch.dict('os.environ', {'FINANCE_AUTH_ENABLED': 'false', 'FINANCE_DEPLOYMENT': 'local'}))
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'test.db'
        self.conn=init_db(self.path)
        self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('a','Bank','current','Bank')")
        self.conn.execute("INSERT INTO transactions(account_id,txn_date,description,merchant,amount,category,dedupe_key) VALUES('a','2026-01-05','Example cafe','Cafe',-100,'Eating out','one')")
        self.conn.commit()
        self.patch=patch.object(main,'DB_PATH',self.path)
        self.patch.start()
        self.client=TestClient(main.app)

    def tearDown(self):
        self.patch.stop()
        self.conn.close()
        self.temp.cleanup()

    def test_resolution_persists_without_removing_spending_and_can_reopen(self):
        self.conn.execute("INSERT INTO transactions(account_id,txn_date,description,merchant,amount,category,dedupe_key) VALUES('a','2026-01-06','Example cafe','Cafe',-100,'Eating out','two')")
        self.conn.commit()
        ident = self.client.get('/api/anomalies').json()[0]['id']
        url = f'/api/transactions/{ident}/review'
        self.assertEqual(self.client.post(url, json={'note': 'Existing note'}).status_code, 200)
        before = self.client.get('/api/summary').json()['spending']
        self.assertTrue(self.client.get('/api/anomalies').json())
        self.assertEqual(self.client.post(url, json={'resolution': 'expected'}).status_code, 200)
        self.assertEqual(self.client.get('/api/anomalies').json(), [])
        self.assertEqual(self.client.get('/api/summary').json()['spending'], before)
        review = self.client.get(url).json()['review']
        self.assertEqual(review['note'], 'Existing note')
        self.assertEqual(review['resolution'], 'expected')
        self.client.post('/api/recompute')
        self.assertEqual(self.client.get('/api/anomalies').json(), [])
        history = self.client.get('/api/anomalies?status=resolved').json()
        self.assertEqual(history[0]['id'], ident)
        self.assertEqual(history[0]['outcome'], 'expected')
        self.assertIn('same account', history[0]['explanation'])
        self.client.post(url, json={'category': 'Housing'})
        self.assertEqual(self.client.get('/api/anomalies?status=resolved').json()[0]['category'], 'Housing')
        self.client.post(url, json={'resolution': 'pending', 'category': 'Eating out'})
        self.assertEqual(self.client.get('/api/anomalies?status=resolved').json(), [])
        self.assertTrue(self.client.get('/api/anomalies').json())
        self.assertEqual(self.client.post(url, json={'resolution': 'issue'}).status_code, 200)
        self.assertEqual(self.client.get('/api/anomalies').json(), [])
        self.assertEqual(self.client.get('/api/anomalies?status=resolved').json()[0]['outcome'], 'issue')

    def test_resolution_rejects_unflagged_payment_and_unknown_outcome(self):
        self.assertEqual(self.client.post('/api/transactions/1/review', json={'resolution': 'expected'}).status_code, 409)
        self.assertEqual(self.client.post('/api/transactions/1/review', json={'resolution': 'invalid'}).status_code, 422)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM anomaly_reviews').fetchone()[0], 0)

    def test_all_read_endpoints_and_finite_planning_validation(self):
        for endpoint in ['summary','transactions','timeseries','categories','recurring','transfers','anomalies','advice','analytics','planning','health']:
            response=self.client.get('/api/'+endpoint)
            self.assertEqual(response.status_code,200,(endpoint,response.text))
        for query in ['annual_return=nan','monthly_contribution=inf','years=0','target=-1']:
            self.assertEqual(self.client.get('/api/planning?'+query).status_code,422)

    def test_startup_migrates_existing_database_before_serving(self):
        self.conn.execute('DROP TABLE corporate_actions')
        self.conn.commit()
        with TestClient(main.app) as client:
            self.assertEqual(client.get('/api/summary').status_code, 200)
            self.assertEqual(client.get('/api/transactions').json()['total'], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM corporate_actions').fetchone()[0], 0)

    def test_invalid_filter_ranges_and_false_boolean(self):
        for query in ['date_from=wrong','date_from=2026-02-01&date_to=2026-01-01','min_amount=100&max_amount=10']:
            self.assertEqual(self.client.get('/api/transactions?'+query).status_code,422)
        self.assertEqual(self.client.get('/api/transactions?transfer=false').json()['total'],1)
        self.assertEqual(self.client.get('/api/transactions?transfer=true').json()['total'],0)

    def test_refund_reduces_spending_consistently(self):
        self.conn.execute("INSERT INTO transactions(account_id,txn_date,description,amount,category) VALUES('a','2026-01-06','Cafe refund',20,'Eating out')")
        self.conn.commit()
        summary=self.client.get('/api/summary').json()
        category=self.client.get('/api/categories').json()[0]
        series=self.client.get('/api/timeseries').json()['net'][0]
        self.assertEqual((summary['income'],summary['spending']),(0,-80))
        self.assertEqual(category['spent'],80)
        self.assertEqual(series['spending'],-80)

    def test_rule_priority_deletion_and_override_survival(self):
        low=self.client.post('/api/rules',json={'pattern':'Example','category':'Low','priority':100}).json()
        high=self.client.post('/api/rules',json={'pattern':'Example','category':'High','priority':1}).json()
        def category():
            return self.client.get('/api/transactions').json()['items'][0]['category']
        self.assertEqual(category(),'High')
        self.client.delete('/api/rules/'+str(high['id']))
        self.assertEqual(category(),'Low')
        self.client.post('/api/transactions/1/category',json={'category':'User choice'})
        self.client.delete('/api/rules/'+str(low['id']))
        self.client.post('/api/recompute',json={})
        self.assertEqual(category(),'User choice')

    def test_invalid_rule_is_rejected_without_persisting(self):
        response=self.client.post('/api/rules',json={'pattern':'[','category':'Bad','is_regex':True})
        self.assertEqual(response.status_code,422)
        self.assertEqual(self.client.get('/api/rules').json(),[])

    def test_retired_upload_rejects_files_without_changing_saved_records(self):
        source = Path(self.temp.name) / 'saved-statement.pdf'
        source.write_bytes(b'%PDF-original saved statement')
        before = '\n'.join(self.conn.iterdump())
        with patch('app.ingest.ingest_file') as ingest:
            for filename, content in [('statement.pdf', b'%PDF-test'), ('activity.csv', b'Action,Time\n')]:
                response = self.client.post('/api/upload', files={'files': (filename, content)},
                                            data={'trading212_account': 'isa'})
                self.assertEqual(response.status_code, 410)
                self.assertIn('retired', response.json()['detail'])
            ingest.assert_not_called()
        self.assertEqual('\n'.join(self.conn.iterdump()), before)
        self.assertEqual(source.read_bytes(), b'%PDF-original saved statement')
        self.assertNotIn('/api/upload', main.app.openapi()['paths'])
        self.assertEqual(self.client.get('/api/transactions').json()['total'], 1)

    def test_direct_debits_and_names_are_not_automatically_transfers(self):
        self.assertEqual(auto_category('Standing order rent','Rent',-100),'Housing')
        self.assertNotEqual(auto_category('Direct debit unknown','Unknown',-20),'Transfers & payments')
        self.assertNotEqual(auto_category('Jamie dinner','Dinner',-20),'Transfers & payments')

    def test_analytics_filters_validation_and_coverage_contract(self):
        self.conn.execute("INSERT INTO transactions(account_id,txn_date,description,amount,category) VALUES('a','2026-02-05','Later cafe',-20,'Eating out')")
        self.conn.commit()
        result = self.client.get('/api/analytics?account=a&date_from=2026-02-01&date_to=2026-02-28&exclude_outliers=false').json()
        self.assertEqual(result['stats']['total_spend'], 20)
        self.assertEqual(result['monthly'][0]['m'], '2026-02')
        self.assertFalse(result['monthly'][0]['coverage_complete'])
        self.assertIn('outliers', result['definitions'])
        for query in ['date_from=2026-02-30', 'date_from=2026-02-01&date_to=2026-01-01', 'outlier_z=nan', 'outlier_z=0']:
            self.assertEqual(self.client.get('/api/analytics?'+query).status_code, 422)
        self.assertEqual(self.client.get('/api/analytics?account=missing').status_code, 404)

    def test_category_options_include_custom_excluded_and_unused_merchant_categories(self):
        self.conn.execute("INSERT INTO transactions(account_id,txn_date,description,amount,category,exclude_from_analysis) VALUES('a','2026-02-05','Excluded legal cost',-20,'Legal services',1)")
        self.conn.execute("INSERT INTO transactions(account_id,txn_date,description,amount,category,spending_treatment) VALUES('a','2026-02-05','Self move',-20,'Own savings','self_transfer')")
        for category, enabled in [('Race events', 1), ('Race events', 1), ('Retired category', 0), ('', 1), ('   ', 1), (None, 1)]:
            self.conn.execute('INSERT INTO merchants(canonical_name,category,enabled) VALUES(?,?,?)', ('Example', category, enabled))
        self.conn.commit()
        response = self.client.get('/api/category-options')
        self.assertEqual(response.status_code, 200, response.text)
        options = response.json()
        self.assertTrue({'Eating out', 'Legal services', 'Own savings', 'Race events'}.issubset(options))
        self.assertEqual(options.count('Race events'), 1)
        self.assertFalse({'Retired category', '', '   ', None}.intersection(options))
        self.assertEqual(options, sorted(options, key=lambda value: (value.casefold(), value)))
        # The existing endpoint still reports household-spending totals only.
        categories = self.client.get('/api/categories').json()
        self.assertFalse(any(row['category'] in {'Legal services', 'Own savings', 'Race events'} for row in categories))

    def test_planning_options_and_saved_scenarios_roundtrip(self):
        response = self.client.get('/api/planning?years=1&initial_wealth=1000&monthly_contribution=100&annual_return=0&inflation=0&one_off_amount=500&one_off_month=12&baseline_method=median&exclude_outliers=true')
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result['scenarios'][1]['points'][-1]['nominal'], 1700)
        self.assertEqual(result['assumptions']['baseline_method'], 'median')
        self.assertTrue(result['assumptions']['exclude_outliers'])
        for query in ['years=1&one_off_month=13', 'one_off_amount=-1', 'baseline_method=mode']:
            self.assertEqual(self.client.get('/api/planning?'+query).status_code, 422)
        params = {'years': 2, 'monthly_contribution': -100, 'baseline_method': 'median',
                  'exclude_outliers': True, 'one_off_amount': 500, 'one_off_month': 12}
        created = self.client.post('/api/scenarios', json={'name': 'Deposit plan', 'parameters': params})
        self.assertEqual(created.status_code, 200)
        item = created.json()
        self.assertEqual(item['parameters']['monthly_contribution'], -100)
        listed = self.client.get('/api/scenarios').json()
        self.assertEqual(len(listed), 1)
        changed = self.client.post('/api/scenarios', json={'id': item['id'], 'name': 'New name', 'parameters': params})
        self.assertEqual(changed.json()['id'], item['id'])
        self.assertEqual(len(self.client.get('/api/scenarios').json()), 1)
        self.assertEqual(self.client.post('/api/scenarios', json={'name': 'Invalid', 'parameters': {'years': 1, 'one_off_month': 13}}).status_code, 422)
        self.assertEqual(self.client.post('/api/scenarios', json={'name': 'Invalid', 'parameters': {'arbitrary': 5}}).status_code, 422)
        self.assertEqual(self.client.delete(f"/api/scenarios/{item['id']}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/scenarios/{item['id']}").status_code, 404)
        self.assertEqual(self.client.get('/api/scenarios').json(), [])

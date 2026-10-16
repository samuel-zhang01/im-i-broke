import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import main, analytics
from app.db import init_db


class RecurringIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Functional API fixtures use local mode; auth/security have dedicated suites.
        self.enterContext(patch.dict('os.environ', {'FINANCE_AUTH_ENABLED': 'false', 'FINANCE_DEPLOYMENT': 'local'}))
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'ledger.db'
        self.conn = init_db(self.path)
        for ident in ('a', 'b'):
            self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES(?, 'Bank','current',?)", (ident, 'Account '+ident))
        self.conn.commit()
        self.patch = patch.object(main, 'DB_PATH', self.path)
        self.patch.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.patch.stop()
        self.conn.close()
        self.temp.cleanup()

    def txn(self, name, day, account='a', amount=-10, category='Uncategorized', **extras):
        ident = self.conn.execute('INSERT INTO transactions(account_id,txn_date,description,source_merchant,merchant,amount,category,dedupe_key) VALUES(?,?,?,?,?,?,?,?)',
            (account, day, name, name, name, amount, category, f'{account}-{name}-{day}-{amount}')).lastrowid
        for field, value in extras.items():
            assert field in ('exclude_from_analysis', 'spending_treatment')
            self.conn.execute(f'UPDATE transactions SET {field}=? WHERE id=?', (value, ident))
        self.conn.commit()
        return ident

    def details(self, params=None):
        response = self.client.get('/api/recurring', params={'details': 'true', **(params or {})})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_alias_classification_connects_pattern_markers_and_original_payments(self):
        ids = [self.txn(name, day) for name, day in [('Service Web','2026-01-05'),('Processor Service','2026-02-05'),('Service Web','2026-03-05')]]
        self.assertEqual(self.details()['patterns'], [])
        result = self.client.post('/api/merchants', json={
            'canonical_name': 'Confirmed Service', 'kind': 'merchant', 'category': 'Subscriptions',
            'aliases': [{'pattern': name, 'match_mode': 'exact', 'context': 'spending'} for name in ('Service Web','Processor Service')]})
        self.assertEqual(result.status_code, 200, result.text)
        data = self.details()
        pattern = data['patterns'][0]
        self.assertEqual(pattern['merchant'], 'Confirmed Service')
        self.assertEqual(pattern['account'], 'Account a')
        self.assertEqual(set(pattern['txn_ids']), set(ids))
        self.assertEqual({p['id'] for p in pattern['payments']}, set(ids))
        self.assertEqual({p['description'] for p in pattern['payments']}, {'Service Web','Processor Service'})
        self.assertEqual(data['candidates'], [])
        marked = self.client.get('/api/transactions?recurring=true').json()['items']
        self.assertEqual({p['id'] for p in marked}, set(ids))
        self.client.post('/api/recompute', json={})
        self.assertEqual(self.details()['patterns'][0]['key'], pattern['key'])
        # The old list response remains compatible with existing consumers.
        self.assertIsInstance(self.client.get('/api/recurring').json(), list)

    def test_unconfirmed_subscription_stays_visible_without_invented_schedule(self):
        ident = self.txn('Annual service', '2026-01-05', category='Subscriptions')
        self.txn('Annual service', '2026-01-06', amount=10, category='Subscriptions')
        self.txn('Hidden service', '2026-01-06', category='Subscriptions', exclude_from_analysis=1)
        self.txn('Transfer service', '2026-01-06', category='Subscriptions', spending_treatment='self_transfer')
        data = self.details()
        self.assertEqual(data['patterns'], [])
        self.assertEqual(len(data['candidates']), 1)
        candidate = data['candidates'][0]
        self.assertEqual(candidate['txn_ids'], [ident])
        self.assertNotIn('monthly_cost', candidate)
        self.assertNotIn('cadence', candidate)

    def test_custom_preview_keeps_accounts_separate_and_does_not_change_markers(self):
        for account in ('a', 'b'):
            for day in ('2026-01-05','2026-02-05'):
                self.txn('Same service', day, account=account, category='Subscriptions')
        analytics.recompute_all(self.conn)
        self.assertEqual(len(self.details()['candidates']), 2)
        preview = self.details({'min_occurrences': 2})
        self.assertEqual(len(preview['patterns']), 2)
        self.assertEqual(preview['candidates'], [])
        for series in preview['patterns']:
            self.assertTrue(all(p['account_id']==series['account_id'] for p in series['payments']))
        self.assertEqual(self.client.get('/api/transactions?recurring=true').json()['total'], 0)

    def test_review_exclusion_updates_evidence_and_subscription_candidates(self):
        ids = [self.txn('Stream', day, category='Subscriptions') for day in ('2026-01-05','2026-02-05','2026-03-05')]
        # Preserve the source category during the normal categorization cycle.
        self.client.post('/api/merchants', json={'canonical_name':'Stream','category':'Subscriptions','aliases':[{'pattern':'Stream','match_mode':'exact','context':'spending'}]})
        self.assertEqual(len(self.details()['patterns']), 1)
        result = self.client.post(f'/api/transactions/{ids[0]}/review', json={'exclude_from_analysis': True})
        self.assertEqual(result.status_code, 200, result.text)
        data = self.details()
        self.assertEqual(data['patterns'], [])
        self.assertEqual(set(data['candidates'][0]['txn_ids']), set(ids[1:]))
        self.assertEqual(self.client.get('/api/transactions?recurring=true').json()['total'], 0)

    def test_recurring_exchange_fee_explains_its_contribution_without_changing_original_amount(self):
        ids = [self.txn('Currency exchange', day, amount=-100, category='Transfers & payments', spending_treatment='self_transfer')
               for day in ('2026-01-05', '2026-02-05', '2026-03-05')]
        self.conn.execute('UPDATE transactions SET meta=?', ('{"currency_exchange":true,"exchange_fee_gbp":1}',))
        self.conn.commit()
        pattern = self.details()['patterns'][0]
        self.assertEqual(pattern['category'], 'Fees & interest')
        self.assertEqual(pattern['typical_amount'], 1)
        self.assertEqual(set(pattern['txn_ids']), set(ids))
        for payment in pattern['payments']:
            self.assertEqual(payment['amount'], -100)
            self.assertEqual(payment['category'], 'Transfers & payments')
            self.assertEqual(payment['analysis_amount'], -1)
            self.assertEqual(payment['analysis_category'], 'Fees & interest')
            self.assertEqual(payment['analysis_component'], 'Printed currency exchange fee')
        self.assertEqual(self.conn.execute('SELECT SUM(amount) FROM transactions').fetchone()[0], -300)

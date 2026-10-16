import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main, merchants
from app.categorize import apply_categorization
from app.db import init_db


class MerchantDirectoryGuardsTests(unittest.TestCase):
    def setUp(self):
        # Functional API fixtures use local mode; auth/security have dedicated suites.
        self.enterContext(patch.dict('os.environ', {'FINANCE_AUTH_ENABLED': 'false', 'FINANCE_DEPLOYMENT': 'local'}))
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'directory.db'
        self.conn = init_db(self.path)
        self.conn.executemany('INSERT INTO accounts(id,bank,account_type,display_name) VALUES(?,?,?,?)', [
            ('card', 'Test', 'credit', 'Test card'), ('bank', 'Test', 'current', 'Test bank'),
            ('broker', 'Trading212', 'investment', 'Broker'),
        ])
        self.rule = self.conn.execute("INSERT INTO merchants(canonical_name,kind,category,source,notes) VALUES('Corner Cafe','merchant','Eating out','public_research','Keep research')").lastrowid
        self.original_alias = self.conn.execute("INSERT INTO merchant_aliases(merchant_id,pattern,match_mode,context) VALUES(?,'Corner Cafe','prefix','spending')", (self.rule,)).lastrowid
        self.conn.commit()
        self.path_patch = patch.object(main, 'DB_PATH', self.path)
        self.path_patch.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.path_patch.stop()
        self.conn.close()
        self.temp.cleanup()

    def add(self, label, account='card', description=None, code=None):
        ident = self.conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM transactions').fetchone()[0]
        self.conn.execute("INSERT INTO transactions(id,account_id,txn_date,description,merchant,source_merchant,amount,category,type_code,raw,meta,dedupe_key) VALUES(?,?,'2026-01-01',?,?,?,-12,'Uncategorized',?,'Original text',?,?)", (ident, account, description or label, label, label, code, json.dumps({'source_merchant': label}), f'row:{ident}'))
        self.conn.commit()
        return ident

    def rule_body(self):
        rule = next(rule for rule in self.client.get('/api/merchants').json()['items'] if rule['id'] == self.rule)
        return {**rule, 'expected_revision': rule['revision']}

    def update(self, body):
        return self.client.put(f'/api/merchants/{self.rule}', json=body)

    def test_revision_ignores_derived_counts_and_detects_alias_or_definition_edits(self):
        body = self.rule_body()
        changed_count = {**body, 'n_transactions': 999}
        changed_count.pop('expected_revision')
        self.assertEqual(merchants.merchant_revision(changed_count), body['revision'])
        self.add('Corner Cafe branch')
        apply_categorization(self.conn)
        self.conn.commit()
        self.assertEqual(self.rule_body()['revision'], body['revision'])
        self.conn.execute("UPDATE merchants SET notes='Changed elsewhere' WHERE id=?", (self.rule,))
        self.conn.commit()
        stale = self.update({**body, 'category': 'Shopping'})
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(tuple(self.conn.execute('SELECT category,notes FROM merchants WHERE id=?', (self.rule,)).fetchone()), ('Eating out', 'Changed elsewhere'))

    def test_stale_directory_save_cannot_remove_concurrently_approved_alias(self):
        body = self.rule_body()
        ident = self.add('Corner Caffe')
        approved = self.client.post(f'/api/merchants/{self.rule}/aliases', json={'pattern': 'Corner Caffe', 'context': 'card', 'example_id': ident})
        self.assertEqual(approved.status_code, 200, approved.text)
        response = self.update(body)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM merchant_aliases WHERE merchant_id=? AND pattern='Corner Caffe'", (self.rule,)).fetchone())

    def test_alias_only_save_preserves_retained_ids_source_and_row_reviews(self):
        first = self.add('Corner Caffe')
        approved = self.client.post(f'/api/merchants/{self.rule}/aliases', json={'pattern': 'Corner Caffe', 'context': 'card', 'example_id': first}).json()
        second = self.add('Corner Kafe')
        broad = self.add('Corner Cafe branch')
        review = self.client.post(f'/api/transactions/{second}/review', json={'category': 'Client meal', 'note': 'Retain this'})
        self.assertEqual(review.status_code, 200)
        immutable_sql = 'SELECT id,account_id,txn_date,description,source_merchant,amount,raw,meta,dedupe_key FROM transactions ORDER BY id'
        before = [tuple(row) for row in self.conn.execute(immutable_sql)]
        body = self.rule_body()
        body['aliases'].append({'pattern': 'Corner Kafe', 'match_mode': 'exact', 'context': 'spending_card'})
        result = self.update(body)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual([tuple(row) for row in self.conn.execute(immutable_sql)], before)
        self.assertIsNotNone(self.conn.execute('SELECT 1 FROM merchant_aliases WHERE id=?', (self.original_alias,)).fetchone())
        self.assertIsNotNone(self.conn.execute('SELECT 1 FROM merchant_alias_approvals WHERE alias_id=?', (approved['alias_id'],)).fetchone())
        self.assertEqual(tuple(self.conn.execute('SELECT source,notes FROM merchants WHERE id=?', (self.rule,)).fetchone()), ('public_research', 'Keep research'))
        self.assertEqual(self.conn.execute('SELECT category FROM transactions WHERE id=?', (second,)).fetchone()[0], 'Client meal')
        self.assertEqual(json.loads(self.conn.execute('SELECT classification FROM transactions WHERE id=?', (broad,)).fetchone()[0])['source'], 'public_research')
        self.assertNotEqual(result.json()['merchant']['revision'], body['revision'])

    def test_normalized_retained_alias_keeps_approval_and_explicit_removal_clears_it(self):
        ident = self.add('Corner Caffe')
        approval = self.client.post(f'/api/merchants/{self.rule}/aliases', json={'pattern': 'Corner Caffe', 'context': 'card', 'example_id': ident}).json()
        body = self.rule_body()
        next(alias for alias in body['aliases'] if alias['id'] == approval['alias_id'])['pattern'] = '  CORNER-CAFFE  '
        self.assertEqual(self.update(body).status_code, 200)
        self.assertIsNotNone(self.conn.execute('SELECT 1 FROM merchant_alias_approvals WHERE alias_id=?', (approval['alias_id'],)).fetchone())
        body = self.rule_body()
        body['aliases'] = [alias for alias in body['aliases'] if alias['id'] != approval['alias_id']]
        self.assertEqual(self.update(body).status_code, 200)
        self.assertIsNone(self.conn.execute('SELECT 1 FROM merchant_alias_approvals WHERE alias_id=?', (approval['alias_id'],)).fetchone())
        self.assertIsNone(self.conn.execute("SELECT 1 FROM merchant_suggestion_decisions WHERE merchant_id=? AND label_key='corner caffe'", (self.rule,)).fetchone())

    def test_funding_overlap_and_failed_create_leave_no_partial_writes(self):
        other = self.conn.execute("INSERT INTO merchants(canonical_name,kind,category) VALUES('Provider funding','financial','Transfers & payments')").lastrowid
        self.conn.execute("INSERT INTO merchant_aliases(merchant_id,pattern,context) VALUES(?,'Provider example','funding')", (other,))
        self.conn.commit()
        body = self.rule_body()
        body['category'] = 'Shopping'
        body['aliases'].append({'pattern': 'PROVIDER-EXAMPLE', 'match_mode': 'exact', 'context': 'spending_transfer'})
        before = self.rule_body()
        response = self.update(body)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn('Provider funding', response.json()['detail'])
        self.assertEqual(self.rule_body(), before)
        count = self.conn.execute('SELECT COUNT(*) FROM merchants').fetchone()[0]
        response = self.client.post('/api/merchants', json={'canonical_name': 'Duplicate provider', 'aliases': [{'pattern': 'Provider example', 'context': 'spending_card'}]})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM merchants').fetchone()[0], count)

    def test_distinct_card_transfer_scopes_allowed_but_actual_overlap_rejected(self):
        other = self.conn.execute("INSERT INTO merchants(canonical_name,kind) VALUES('Transfer recipient','merchant')").lastrowid
        self.conn.execute("INSERT INTO merchant_aliases(merchant_id,pattern,context) VALUES(?,'Shared label','transfer')", (other,))
        self.conn.commit()
        self.add('Shared label')
        self.add('Shared label', account='bank', description='To Shared label', code='BP')
        body = self.rule_body()
        body['aliases'].append({'pattern': 'Shared label', 'match_mode': 'exact', 'context': 'spending_card'})
        response = self.update(body)
        self.assertEqual(response.status_code, 200, response.text)
        ambiguous = self.conn.execute("INSERT INTO merchant_aliases(merchant_id,pattern,context) VALUES(?,'Ambiguous label','transfer')", (other,)).lastrowid
        self.conn.commit()
        self.add('Ambiguous label', account='bank', description='To Ambiguous label', code='CARD')
        body = self.rule_body()
        body['aliases'].append({'pattern': 'Ambiguous label', 'match_mode': 'exact', 'context': 'card'})
        response = self.update(body)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIsNotNone(self.conn.execute('SELECT 1 FROM merchant_aliases WHERE id=?', (ambiguous,)).fetchone())

    def test_concurrent_edits_from_same_revision_keep_winner_and_reject_stale_draft(self):
        body = self.rule_body()
        edits = [{**body, 'aliases': [*body['aliases'], {'pattern': name, 'match_mode': 'exact', 'context': 'card'}]} for name in ['Corner Kafe', 'Corner Caffe']]
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(self.update, edits))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM merchant_aliases WHERE merchant_id=?', (self.rule,)).fetchone()[0], 2)

    def test_another_exact_description_owner_cannot_be_reassigned_by_source_label(self):
        self.add('Corner Caffe', description='Corner Caffe London')
        body = self.rule_body()
        # This decision happened after the editor's suggestions were loaded.
        other = self.conn.execute("INSERT INTO merchants(canonical_name,kind) VALUES('Different London cafe','merchant')").lastrowid
        self.conn.execute("INSERT INTO merchant_aliases(merchant_id,pattern,context) VALUES(?,'Corner Caffe London','card')", (other,))
        self.conn.commit()
        body['aliases'].append({'pattern': 'Corner Caffe', 'match_mode': 'exact', 'context': 'spending_card'})
        response = self.update(body)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn('Different London cafe', response.json()['detail'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM merchant_aliases WHERE merchant_id=?', (self.rule,)).fetchone()[0], 1)

    def test_legacy_clients_may_omit_revision_and_explicit_definition_edit_is_user_sourced(self):
        body = self.rule_body()
        body.pop('expected_revision')
        body['category'] = 'Coffee breaks'
        response = self.update(body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['merchant']['source'], 'user')
        self.assertEqual(response.json()['merchant']['category'], 'Coffee breaks')


if __name__ == '__main__':
    unittest.main()

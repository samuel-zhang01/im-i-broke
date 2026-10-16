import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main, merchant_merge, merchants
from app.categorize import apply_categorization
from app.db import init_db


class MerchantMergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'merge.db'
        self.conn = init_db(self.path)
        self.conn.executemany('INSERT INTO accounts(id,bank,account_type,display_name) VALUES(?,?,?,?)', [
            ('bank', 'Sample bank', 'current', 'Current'), ('card', 'Amex', 'credit', 'Card')])
        self.conn.commit()
        self.db_patch = patch.object(main, 'DB_PATH', self.path)
        self.db_patch.start()
        self.auth_patch = patch.dict(os.environ, {'FINANCE_AUTH_ENABLED': 'false'})
        self.auth_patch.start()
        self.client = TestClient(main.app, headers={'X-Ledger-Request': '1'})

    def tearDown(self):
        self.db_patch.stop()
        self.auth_patch.stop()
        self.conn.close()
        self.temp.cleanup()

    def merchant(self, name, aliases, *, kind='merchant', category='Eating out',
                 treatment='normal', source='user', seed=None):
        ident = self.conn.execute('''INSERT INTO merchants(canonical_name,kind,category,
            spending_treatment,source,seed_key,source_url,notes) VALUES(?,?,?,?,?,?,?,?)''',
            (name, kind, category, treatment, source, seed, 'https://example.test/' + str(seed or 'cafe'),
             'Original source notes for ' + name)).lastrowid
        for alias in aliases:
            pattern, mode, context = alias if isinstance(alias, tuple) else (alias, 'exact', 'any')
            self.conn.execute('INSERT INTO merchant_aliases(merchant_id,pattern,match_mode,context) VALUES(?,?,?,?)',
                              (ident, pattern, mode, context))
        self.conn.commit()
        return ident

    def txn(self, description, *, account='bank', amount=-12, code='BP', key=None):
        ident = self.conn.execute('''INSERT INTO transactions(account_id,txn_date,description,
            merchant,source_merchant,amount,type_code,category,dedupe_key,raw,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            (account, '2026-03-04', description, description, description, amount, code,
             'Uncategorized', key or description + account, 'Original bank statement text', '{}')).lastrowid
        apply_categorization(self.conn)
        self.conn.commit()
        return ident

    def read(self, ident):
        return dict(self.conn.execute('SELECT * FROM transactions WHERE id=?', (ident,)).fetchone())

    def preview(self, target, *sources):
        response = self.client.post('/api/merchants/merge/preview', json={'target_id': target, 'source_ids': list(sources)})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def merge(self, target, *sources, preview=None):
        data = preview or self.preview(target, *sources)
        response = self.client.post('/api/merchants/merge', json={
            'target_id': target, 'source_ids': list(sources), 'preview_token': data['preview_token']})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_confirmed_merge_preserves_sources_reviews_scopes_and_future_imports(self):
        target = self.merchant('Alex Jordan Morgan', [('alex jordan morgan', 'exact', 'transfer')],
                               kind='person', category='Friends & shared costs')
        source = self.merchant('Alex Morgan', [('alex morgan deposit refund', 'exact', 'transfer')],
                               kind='person', category='Friends & shared costs', seed='personal-test-seed')
        primary = self.txn('Transfer to ALEX JORDAN MORGAN')
        secondary = self.txn('Alex Morgan Deposit Refund')
        reviewed = self.txn('Alex Morgan Deposit Refund', key='reviewed-row')
        response = self.client.post(f'/api/transactions/{reviewed}/review', json={
            'merchant': 'Shared dinner', 'category': 'Shopping', 'merchant_kind': 'person',
            'spending_treatment': 'reimbursement', 'exclude_from_analysis': True, 'note': 'Keep my decision'})
        self.assertEqual(response.status_code, 200, response.text)
        raw_fields = ('txn_date', 'description', 'amount', 'raw', 'meta', 'dedupe_key', 'account_id', 'source_merchant')
        original = {ident: tuple(self.read(ident)[field] for field in raw_fields) for ident in (primary, secondary, reviewed)}
        review_before = dict(self.conn.execute('SELECT * FROM overrides').fetchone())
        preview = self.preview(target, source)
        self.assertEqual((preview['matched_transactions'], preview['changed_transactions'], preview['preserved_reviews']), (3, 2, 1))
        self.assertEqual(self.read(secondary)['merchant'], 'Alex Morgan')
        with patch.object(merchant_merge.analytics, 'recompute_all', wraps=merchant_merge.analytics.recompute_all) as recompute:
            result = self.merge(target, source, preview=preview)
        self.assertEqual(recompute.call_count, 1)
        self.assertEqual(result['updated_transactions'], preview['changed_transactions'])
        self.assertEqual(self.read(secondary)['merchant'], 'Alex Jordan Morgan')
        self.assertEqual(self.read(reviewed)['merchant'], 'Shared dinner')
        self.assertEqual(self.read(reviewed)['spending_treatment'], 'reimbursement')
        self.assertEqual(dict(self.conn.execute('SELECT * FROM overrides').fetchone()), review_before)
        for ident, values in original.items():
            self.assertEqual(tuple(self.read(ident)[field] for field in raw_fields), values)
        self.assertEqual(self.conn.execute('SELECT enabled FROM merchants WHERE id=?', (source,)).fetchone()[0], 0)
        future = self.txn('Alex Morgan Deposit Refund', key='future')
        unrelated = self.txn('Alex Morgan Deposit Refund', account='card', code='CARD', key='unrelated')
        self.assertEqual(self.read(future)['merchant'], 'Alex Jordan Morgan')
        self.assertEqual(self.read(unrelated)['merchant_kind'], 'unknown')
        audit = self.client.get(f'/api/merchants/{target}/merge-history').json()['items'][0]
        self.assertEqual(audit['source_ids'], [source])
        self.assertIn('Original source notes', audit['snapshot']['sources'][0]['notes'])
        self.assertEqual(audit['snapshot']['sources'][0]['source_url'], 'https://example.test/personal-test-seed')

    def test_normalized_duplicates_merge_approvals_without_losing_scopes(self):
        target = self.merchant('Bramble Cafe', [('Sq Bramble London', 'exact', 'card'),
                                              ('SQ BRAMBLE LONDON', 'exact', 'spending_card')], source='public_research')
        source = self.merchant('Bramble Cafe', [('SQ BRAMBLE LONDON', 'exact', 'card'),
                                              ('Bramble', 'prefix', 'card')], source='public_research')
        target_alias = self.conn.execute('SELECT id FROM merchant_aliases WHERE merchant_id=? ORDER BY id', (target,)).fetchone()[0]
        source_alias = self.conn.execute('SELECT id FROM merchant_aliases WHERE merchant_id=? ORDER BY id', (source,)).fetchone()[0]
        self.conn.execute('INSERT INTO merchant_alias_approvals(alias_id) VALUES(?)', (source_alias,))
        merchants.set_suggestion_decision(self.conn, target, 'SQ BRAMBLE LONDON', 'dismissed')
        merchants.set_suggestion_decision(self.conn, source, 'Sq Bramble London', 'approved')
        merchants.set_suggestion_decision(self.conn, source, 'Brambl Cafe', 'dismissed')
        self.conn.commit()
        preview = self.preview(target, source)
        self.assertEqual(preview['alias_count'], 3)
        self.assertTrue(any('prefix' in warning for warning in preview['warnings']))
        self.merge(target, source, preview=preview)
        aliases = [dict(row) for row in self.conn.execute('SELECT * FROM merchant_aliases WHERE merchant_id=?', (target,))]
        self.assertEqual(len(aliases), 3)
        self.assertEqual({(row['match_mode'], row['context']) for row in aliases}, {('exact', 'card'), ('exact', 'spending_card'), ('prefix', 'card')})
        # The approved source alias is the original strongest classifier rule,
        # so retaining its ID also preserves priority against unrelated names.
        self.assertIsNotNone(self.conn.execute('SELECT * FROM merchant_alias_approvals WHERE alias_id=?', (source_alias,)).fetchone())
        self.assertIsNone(self.conn.execute('SELECT * FROM merchant_aliases WHERE id=?', (target_alias,)).fetchone())
        decisions = {row['label_key']: row['status'] for row in self.conn.execute('SELECT * FROM merchant_suggestion_decisions WHERE merchant_id=?', (target,))}
        self.assertEqual(decisions, {'sq bramble london': 'approved', 'brambl cafe': 'dismissed'})
        audit = self.client.get(f'/api/merchants/{target}/merge-history').json()['items'][0]['snapshot']
        self.assertEqual(audit['approvals'][0]['alias_id'], source_alias)
        self.assertEqual(len(audit['decisions']), 3)

    def test_category_warning_keeps_target_settings(self):
        target = self.merchant('Bramble Home', ['BRAMBLE HOME'], category='Shopping')
        source = self.merchant('Bramble Home London', ['BRAMBLE HOME LONDON'], category='Groceries')
        ident = self.txn('BRAMBLE HOME LONDON')
        preview = self.preview(target, source)
        self.assertTrue(any('Categories differ' in message for message in preview['warnings']))
        self.assertEqual(preview['changed_transactions'], 1)
        self.merge(target, source, preview=preview)
        self.assertEqual(self.read(ident)['category'], 'Shopping')
        self.assertEqual(self.conn.execute('SELECT source FROM merchants WHERE id=?', (target,)).fetchone()[0], 'user')

    def test_implicit_user_exact_approval_retains_priority_after_merge_into_research(self):
        competitor = self.merchant('Bramble legacy', ['BRAMBLE CARD'], source='public_research')
        target = self.merchant('Bramble Cafe', ['BRAMBLE CAFE'], source='public_research')
        source = self.merchant('Bramble current', ['BRAMBLE CARD'], source='user')
        ident = self.txn('BRAMBLE CARD')
        self.assertEqual(json.loads(self.read(ident)['classification'])['merchant_id'], source)
        self.merge(target, source)
        self.assertEqual(json.loads(self.read(ident)['classification'])['merchant_id'], target)
        self.assertEqual(json.loads(self.read(ident)['classification'])['source'], 'user')
        self.assertTrue(self.conn.execute('SELECT enabled FROM merchants WHERE id=?', (competitor,)).fetchone()[0])

    def test_overlap_preserves_matches_owned_by_an_unselected_name(self):
        competitor = self.merchant('Bramble Other', [('BRAMBLE', 'prefix', 'card')], source='public_research')
        source = self.merchant('Bramble Long', [('BRAMBLE', 'prefix', 'card')], source='public_research')
        target = self.merchant('Bramble Cafe', ['BRAMBLE CAFE'], source='user')
        ident = self.txn('BRAMBLE GARDENS', account='card', code='CARD')
        self.assertEqual(json.loads(self.read(ident)['classification'])['merchant_id'], competitor)
        preview = self.preview(target, source)
        self.assertEqual(preview['changed_transactions'], 0)
        self.merge(target, source, preview=preview)
        self.assertEqual(json.loads(self.read(ident)['classification'])['merchant_id'], competitor)

    def test_unobserved_aliases_retain_public_priority_in_user_destination(self):
        for mode, description in [('prefix', 'BRAMBLE GARDENS'), ('exact', 'BRAMBLE EXACT')]:
            with self.subTest(mode=mode):
                pattern = 'BRAMBLE' if mode == 'prefix' else description
                competitor = self.merchant('Bramble other ' + mode, [(pattern, mode, 'card')], source='public_research')
                source = self.merchant('Bramble source ' + mode, [(pattern, mode, 'card')], source='public_research')
                target = self.merchant('Bramble target ' + mode, [('ELSEWHERE ' + mode, 'exact', 'card')])
                row = {'description': description, 'account_type': 'credit', 'type_code': 'CARD'}
                self.assertEqual(merchants.classify(row, merchants._prepared(self.conn))['classification']['merchant_id'], competitor)
                preview = self.preview(target, source)
                self.assertEqual(preview['matched_transactions'], 0)
                self.merge(target, source, preview=preview)
                self.assertEqual(merchants.classify(row, merchants._prepared(self.conn))['classification']['merchant_id'], competitor)
                imported = self.txn(description, account='card', code='CARD')
                self.assertEqual(json.loads(self.read(imported)['classification'])['merchant_id'], competitor)

    def test_duplicate_merge_retains_oldest_winning_id_for_future_transactions(self):
        source = self.merchant('Bramble source', [('BRAMBLE GARDENS', 'exact', 'card')])
        competitor = self.merchant('Bramble competitor', [('BRAMBLE GARDENS', 'exact', 'card')])
        target = self.merchant('Bramble target', [('BRAMBLE GARDENS', 'exact', 'card')])
        source_alias = self.conn.execute('SELECT id FROM merchant_aliases WHERE merchant_id=?', (source,)).fetchone()[0]
        target_alias = self.conn.execute('SELECT id FROM merchant_aliases WHERE merchant_id=?', (target,)).fetchone()[0]
        row = {'description': 'BRAMBLE GARDENS', 'account_type': 'credit', 'type_code': 'CARD'}
        self.assertEqual(merchants.classify(row, merchants._prepared(self.conn))['classification']['merchant_id'], source)
        preview = self.preview(target, source)
        self.assertEqual([alias['id'] for alias in preview['aliases']], [source_alias])
        self.merge(target, source, preview=preview)
        self.assertIsNone(self.conn.execute('SELECT id FROM merchant_aliases WHERE id=?', (target_alias,)).fetchone())
        self.assertEqual(self.conn.execute('SELECT merchant_id FROM merchant_aliases WHERE id=?', (source_alias,)).fetchone()[0], target)
        imported = self.txn('BRAMBLE GARDENS', account='card', code='CARD')
        self.assertEqual(json.loads(self.read(imported)['classification'])['merchant_id'], target)
        self.assertNotEqual(json.loads(self.read(imported)['classification'])['merchant_id'], competitor)

    def test_inherited_priority_survives_alias_only_edits_and_clears_on_definition_edit(self):
        source = self.merchant('Bramble source', ['BRAMBLE PUBLIC'], source='public_research')
        target = self.merchant('Bramble target', ['BRAMBLE USER'])
        source_alias = self.conn.execute('SELECT id FROM merchant_aliases WHERE merchant_id=?', (source,)).fetchone()[0]
        self.merge(target, source)
        retained = next(item for item in merchants.list_merchants(self.conn) if item['id'] == target)
        response = self.client.put(f'/api/merchants/{target}', json={**retained, 'expected_revision': retained['revision'],
            'aliases': [*retained['aliases'], {'pattern': 'BRAMBLE NEW'}]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.conn.execute('SELECT source FROM merchant_alias_sources WHERE alias_id=?', (source_alias,)).fetchone()[0], 'public_research')
        retained = response.json()['merchant']
        response = self.client.put(f'/api/merchants/{target}', json={**retained,
            'expected_revision': retained['revision'], 'category': 'Shopping'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self.conn.execute('SELECT source FROM merchant_alias_sources WHERE alias_id=?', (source_alias,)).fetchone())
        alias = next(alias for alias in merchants._prepared(self.conn) if alias['id'] == source_alias)
        self.assertEqual(alias['source'], 'user')

    def test_inherited_source_survives_successive_merges_and_is_audited(self):
        source = self.merchant('Bramble public', ['BRAMBLE PUBLIC'], source='public_research')
        target = self.merchant('Bramble target', ['BRAMBLE TARGET'])
        final = self.merchant('Bramble final', ['BRAMBLE FINAL'])
        source_alias = self.conn.execute('SELECT id FROM merchant_aliases WHERE merchant_id=?', (source,)).fetchone()[0]
        self.merge(target, source)
        self.merge(final, target)
        self.assertEqual(self.conn.execute('SELECT source FROM merchant_alias_sources WHERE alias_id=?', (source_alias,)).fetchone()[0], 'public_research')
        history = self.client.get(f'/api/merchants/{final}/merge-history').json()['items']
        self.assertIn({'alias_id': source_alias, 'source': 'public_research'}, history[0]['snapshot']['alias_sources'])
        self.conn.execute('DELETE FROM merchant_aliases WHERE id=?', (source_alias,))
        self.assertIsNone(self.conn.execute('SELECT * FROM merchant_alias_sources WHERE alias_id=?', (source_alias,)).fetchone())

    def test_merge_preserves_legacy_null_and_inconsistent_source_merchant(self):
        source = self.merchant('Bramble old', ['BRAMBLE CARD'])
        target = self.merchant('Bramble Cafe', ['BRAMBLE CAFE'])
        null_id = self.txn('BRAMBLE CARD', key='null-parser-source')
        mismatch_id = self.txn('BRAMBLE CARD', key='mismatched-parser-source')
        self.conn.execute('UPDATE transactions SET source_merchant=NULL WHERE id=?', (null_id,))
        self.conn.execute('UPDATE transactions SET source_merchant=?,meta=? WHERE id=?',
                          ('Original stored label', json.dumps({'source_merchant': 'BRAMBLE CARD'}), mismatch_id))
        self.conn.commit()
        before = {ident: self.read(ident) for ident in (null_id, mismatch_id)}
        preview = self.preview(target, source)
        for ident in before:
            self.assertEqual(self.read(ident), before[ident])
        result = self.merge(target, source, preview=preview)
        self.assertEqual(result['updated_transactions'], preview['changed_transactions'])
        for ident in before:
            current = self.read(ident)
            self.assertEqual(current['merchant'], 'Bramble Cafe')
            for field in ('source_merchant', 'meta', 'description', 'raw', 'amount', 'txn_date'):
                self.assertEqual(current[field], before[ident][field])

    def test_suggestions_endpoint_names_duplicate_records_without_writing(self):
        target = self.merchant('Bramble Cafe', ['BRAMBLE'])
        source = self.merchant('Bramble Cafe', ['BRAMBLE CARD'])
        before = merchant_merge._state(self.conn)
        response = self.client.get('/api/merchants/merge/suggestions?limit=20')
        self.assertEqual(response.status_code, 200, response.text)
        suggestion = next(item for item in response.json()['items']
                          if {item['target_id'], item['source_id']} == {target, source})
        self.assertEqual(suggestion['match_type'], 'same_name')
        self.assertEqual(merchant_merge._state(self.conn), before)
        self.assertEqual(self.client.get('/api/merchants/merge/suggestions?limit=101').status_code, 422)

    def test_preview_is_read_only_and_does_not_recompute(self):
        target = self.merchant('Bramble Cafe', ['BRAMBLE'])
        source = self.merchant('Bramble Cafeteria', ['BRAMBLE CAFETERIA'])
        self.txn('BRAMBLE CAFETERIA')
        before = merchant_merge._state(self.conn)
        with patch.object(merchant_merge.analytics, 'recompute_all') as recompute:
            self.preview(target, source)
        recompute.assert_not_called()
        self.assertEqual(merchant_merge._state(self.conn), before)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM merchant_merge_history').fetchone()[0], 0)

    def test_alias_limit_applies_after_deduplication_and_merged_name_remains_editable(self):
        target = self.merchant('Bramble Cafe', [f'Bramble branch {index}' for index in range(60)])
        source = self.merchant('Bramble Cafeteria', [f'BRAMBLE BRANCH {index}' for index in range(40, 100)])
        preview = self.preview(target, source)
        self.assertEqual(preview['alias_count'], merchants.MAX_ALIASES_PER_MERCHANT)
        self.merge(target, source, preview=preview)
        retained = next(item for item in merchants.list_merchants(self.conn) if item['id'] == target)
        response = self.client.put(f'/api/merchants/{target}', json={**retained,
            'expected_revision': retained['revision'], 'notes': 'Edited after a full-size merge'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()['merchant']['aliases']), 100)
        extra = self.merchant('Bramble final branch', ['BRAMBLE BRANCH 100'])
        before = merchant_merge._state(self.conn)
        for endpoint in ('/api/merchants/merge/preview', '/api/merchants/merge'):
            response = self.client.post(endpoint, json={'target_id': target, 'source_ids': [extra]})
            self.assertEqual(response.status_code, 422, response.text)
            self.assertIn('101 aliases', response.json()['detail'])
        self.assertEqual(merchant_merge._state(self.conn), before)

    def test_merge_with_no_aliases_cannot_create_an_uneditable_empty_rule(self):
        target = self.merchant('Bramble Cafe', [])
        source = self.merchant('Bramble Cafeteria', [])
        response = self.client.post('/api/merchants/merge/preview', json={'target_id': target, 'source_ids': [source]})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn('matching aliases', response.json()['detail'])

    def test_invalid_incompatible_or_disabled_selections_are_rejected(self):
        target = self.merchant('Bramble Cafe', ['BRAMBLE'])
        friend = self.merchant('Alex Morgan', ['ALEX MORGAN'], kind='person')
        transfer = self.merchant('Bramble transfer', ['BRAMBLE FUNDING'], treatment='self_transfer')
        disabled = self.merchant('Bramble Former', ['BRAMBLE FORMER'])
        self.conn.execute('UPDATE merchants SET enabled=0 WHERE id=?', (disabled,))
        self.conn.commit()
        for sources, status in [([target], 422), ([friend], 422), ([transfer], 422),
                                ([friend, friend], 422), ([disabled], 409), ([999999], 409), ([], 422), ([friend] * 21, 422)]:
            with self.subTest(sources=sources):
                response = self.client.post('/api/merchants/merge/preview', json={'target_id': target, 'source_ids': sources})
                self.assertEqual(response.status_code, status, response.text)

    def test_save_requires_fresh_preview_after_any_relevant_state_change(self):
        target = self.merchant('Bramble Cafe', ['BRAMBLE'])
        source = self.merchant('Bramble Cafeteria', ['BRAMBLE CAFETERIA'])
        ident = self.txn('BRAMBLE CAFETERIA')
        response = self.client.post('/api/merchants/merge', json={'target_id': target, 'source_ids': [source]})
        self.assertEqual(response.status_code, 409)
        mutations = [
            ('UPDATE merchants SET notes=? WHERE id=?', ('Updated notes', source)),
            ('UPDATE transactions SET amount=? WHERE id=?', (-100, ident)),
            ('INSERT INTO overrides(dedupe_key,category) VALUES(?,?)', ('BRAMBLE CAFETERIAbank', 'Shopping')),
            ('INSERT INTO rules(pattern,category) VALUES(?,?)', ('BRAMBLE', 'Groceries')),
            ('INSERT INTO merchant_alias_approvals(alias_id) SELECT id FROM merchant_aliases WHERE merchant_id=?', (source,)),
            ('INSERT INTO merchant_alias_sources(alias_id,source) SELECT id,? FROM merchant_aliases WHERE merchant_id=?', ('public_research', source)),
        ]
        for sql, args in mutations:
            with self.subTest(sql=sql):
                preview = self.preview(target, source)
                self.conn.execute(sql, args)
                self.conn.commit()
                response = self.client.post('/api/merchants/merge', json={
                    'target_id': target, 'source_ids': [source], 'preview_token': preview['preview_token']})
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(self.conn.execute('SELECT enabled FROM merchants WHERE id=?', (source,)).fetchone()[0], 1)

    def test_failed_recompute_rolls_back_rules_reviews_transactions_and_audit(self):
        target = self.merchant('Bramble Cafe', ['BRAMBLE'])
        source = self.merchant('Bramble Cafeteria', ['BRAMBLE CAFETERIA'])
        self.txn('BRAMBLE CAFETERIA')
        preview = self.preview(target, source)
        before = merchant_merge._state(self.conn)
        with patch.object(merchant_merge.analytics, 'recompute_all', side_effect=RuntimeError('Simulated failed recompute')):
            with self.assertRaisesRegex(RuntimeError, 'Simulated failed'):
                self.merge(target, source, preview=preview)
        self.assertEqual(merchant_merge._state(self.conn), before)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM merchant_merge_history').fetchone()[0], 0)

    def test_two_concurrent_confirmations_only_merge_once(self):
        target = self.merchant('Bramble Cafe', ['BRAMBLE'])
        source = self.merchant('Bramble Cafeteria', ['BRAMBLE CAFETERIA'])
        self.txn('BRAMBLE CAFETERIA')
        preview = self.preview(target, source)
        body = {'target_id': target, 'source_ids': [source], 'preview_token': preview['preview_token']}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.client.post('/api/merchants/merge', json=body), range(2)))
        self.assertEqual(sorted(response.status_code for response in results), [200, 409])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM merchant_merge_history').fetchone()[0], 1)

    def test_merged_public_and_account_evidence_seeds_do_not_resurrect(self):
        target = self.merchant('Saved coffee shops', ['EXAMPLE COFFEE'])
        public = next(item for item in merchants.list_merchants(self.conn) if item['source'] == 'public_research'
                      and item['kind'] == 'merchant' and item['spending_treatment'] == 'normal')
        self.merge(target, public['id'])
        self.conn.execute("UPDATE accounts SET account_number='378200000001234' WHERE id='card'")
        self.conn.commit()
        payment = self.txn('AMERICAN EXP 3782******1234', amount=-200)
        seeded = next(item for item in merchants.list_merchants(self.conn) if item['seed_key'] and item['seed_key'].startswith('repayment:'))
        repayment = self.merchant('Amex repayments', [], kind='financial', category='Transfers & payments', treatment='self_transfer')
        self.merge(repayment, seeded['id'])
        reopened = init_db(self.path)
        apply_categorization(reopened)
        reopened.commit()
        reopened.close()
        self.assertEqual(self.read(payment)['merchant'], 'Amex repayments')
        for original in (public, seeded):
            rows = self.conn.execute('SELECT id,enabled FROM merchants WHERE seed_key=?', (original['seed_key'],)).fetchall()
            self.assertEqual([tuple(row) for row in rows], [(original['id'], 0)])

    def test_history_remains_inspectable_across_successive_merges(self):
        target = self.merchant('Bramble Cafe', ['BRAMBLE'])
        source = self.merchant('Bramble Cafeteria', ['BRAMBLE CAFETERIA'])
        final = self.merchant('Bramble', ['BRAMBLE CAFE'])
        self.merge(target, source)
        self.merge(final, target)
        history = self.client.get(f'/api/merchants/{final}/merge-history').json()['items']
        self.assertEqual(len(history), 2)
        self.assertEqual({row['target_id'] for row in history}, {target, final})
        self.assertEqual(self.client.get('/api/merchants/999999/merge-history').status_code, 404)


if __name__ == '__main__':
    unittest.main()

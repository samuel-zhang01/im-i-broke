import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main
from app.categorize import apply_categorization
from app.db import init_db


class MerchantInboxTests(unittest.TestCase):
    def setUp(self):
        # Functional API fixtures use local mode; auth/security have dedicated suites.
        self.enterContext(patch.dict('os.environ', {'FINANCE_AUTH_ENABLED': 'false', 'FINANCE_DEPLOYMENT': 'local'}))
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'inbox.db'
        self.conn = init_db(self.path)
        for ident, kind in [('card', 'credit'), ('bank', 'current'), ('broker', 'investment')]:
            self.conn.execute('INSERT INTO accounts(id,bank,account_type,display_name) VALUES(?,?,?,?)', (ident, 'Test', kind, ident))
        self.conn.commit()
        self.path_patch = patch.object(main, 'DB_PATH', self.path)
        self.path_patch.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.path_patch.stop()
        self.conn.close()
        self.temp.cleanup()

    def add(self, label='Tiny Cafe', account='card', description=None, amount=-12, type_code=None):
        description = description or label + ' London'
        next_id = self.conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM transactions').fetchone()[0]
        ident = self.conn.execute("INSERT INTO transactions(account_id,txn_date,description,source_merchant,merchant,amount,category,type_code,raw,meta,dedupe_key) VALUES(?,'2026-01-01',?,?,?,?,'Uncategorized',?,?,?,?)", (account, description, label, label, amount, type_code, 'Original statement text', json.dumps({'source_merchant': label}), f'row:{next_id}')).lastrowid
        self.conn.commit()
        return ident

    def draft(self, ident, **changes):
        return {'example_id': ident, 'canonical_name': 'Tiny Café', 'category': 'Eating out', 'kind': 'merchant', 'spending_treatment': 'normal', **changes}

    def preview(self, draft):
        response = self.client.post('/api/merchants/classify/preview', json=draft)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def save(self, draft):
        preview = self.preview(draft)
        response = self.client.post('/api/merchants/classify', json={**draft, 'preview_token': preview['preview_token']})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_explicit_existing_category_change_previews_all_aliases_and_keeps_reviews(self):
        first, other, reviewed = self.add(), self.add('Other descriptor'), self.add('Other descriptor')
        saved = self.save(self.draft(first, category='Uncategorized'))
        merchant_id = saved['merchant_id']
        self.save(self.draft(other, merchant_id=merchant_id))
        self.client.post(f'/api/transactions/{reviewed}/review', json={'category': 'Client dinner'})
        # Merely attaching an alias still preserves the saved definition.
        unchanged = self.preview(self.draft(first, merchant_id=merchant_id))
        self.assertEqual(unchanged['category'], 'Uncategorized')
        draft = self.draft(first, merchant_id=merchant_id, update_category=True)
        preview = self.preview(draft)
        self.assertEqual((preview['matched_transactions'], preview['preserved_reviews']), (3, 1))
        self.assertTrue(preview['updates_saved_category'])
        self.assertEqual(self.conn.execute('SELECT category FROM merchants WHERE id=?', (merchant_id,)).fetchone()[0], 'Uncategorized')
        result = self.save(draft)
        self.assertEqual(result['updated_transactions'], 2)
        self.assertEqual([r[0] for r in self.conn.execute('SELECT category FROM transactions ORDER BY id')], ['Eating out', 'Eating out', 'Client dinner'])
        future = self.add('Other descriptor')
        apply_categorization(self.conn); self.conn.commit()
        self.assertEqual(self.conn.execute('SELECT category FROM transactions WHERE id=?', (future,)).fetchone()[0], 'Eating out')
        self.assertEqual(self.save(draft)['updated_transactions'], 0)

    def test_existing_category_edit_rejects_stale_preview_and_empty_category(self):
        ident = self.add()
        merchant_id = self.save(self.draft(ident, category='Uncategorized'))['merchant_id']
        draft = self.draft(ident, merchant_id=merchant_id, update_category=True)
        preview = self.preview(draft)
        self.conn.execute("UPDATE merchants SET category='Shopping' WHERE id=?", (merchant_id,)); self.conn.commit()
        response = self.client.post('/api/merchants/classify', json={**draft, 'preview_token': preview['preview_token']})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.conn.execute('SELECT category FROM merchants WHERE id=?', (merchant_id,)).fetchone()[0], 'Shopping')
        self.assertEqual(self.client.post('/api/merchants/classify/preview', json={**draft, 'category': '   '}).status_code, 422)

    def test_inbox_groups_source_labels_contexts_and_excludes_broker_rows(self):
        self.add(); self.add(label='Tiny-Cafe'); self.add(account='broker')
        self.add(account='bank', description='To Tiny Cafe', type_code='BP')
        self.add(label='Refund only', amount=12)
        result = self.client.get('/api/merchants/inbox').json()
        self.assertEqual(result['total_labels'], 2)
        card = next(item for item in result['items'] if item['context'] == 'card')
        self.assertEqual((card['n_transactions'], card['outgoing_total']), (2, 24))
        self.assertEqual(result['uncategorized_labels'], 2)
        self.assertEqual(card['label'], 'Tiny-Cafe')

    def test_preview_is_read_only_and_save_preserves_source_reviews_and_future_imports(self):
        first, reviewed, broker = self.add(), self.add(), self.add(account='broker', type_code='CARD')
        self.client.post(f'/api/transactions/{reviewed}/review', json={'category': 'Client dinner', 'note': 'Business expense'})
        source_fields = 'id,account_id,txn_date,description,source_merchant,amount,raw,meta,dedupe_key'
        before = [tuple(row) for row in self.conn.execute(f'SELECT {source_fields} FROM transactions ORDER BY id')]
        raw_before = [tuple(row) for row in self.conn.execute('SELECT * FROM transactions ORDER BY id')]
        draft = self.draft(first)
        preview = self.preview(draft)
        self.assertEqual((preview['matched_transactions'], preview['preserved_reviews']), (2, 1))
        self.assertEqual([tuple(row) for row in self.conn.execute('SELECT * FROM transactions ORDER BY id')], raw_before)
        result = self.save(draft)
        self.assertEqual(result['updated_transactions'], 2)  # the reviewed category stays; its identity can improve
        self.assertEqual([tuple(row) for row in self.conn.execute(f'SELECT {source_fields} FROM transactions ORDER BY id')], before)
        self.assertEqual(self.conn.execute('SELECT category FROM transactions WHERE id=?', (reviewed,)).fetchone()[0], 'Client dinner')
        self.assertEqual(self.conn.execute('SELECT merchant FROM transactions WHERE id=?', (broker,)).fetchone()[0], 'Tiny Cafe')
        future = self.add(label='TINY CAFE')
        apply_categorization(self.conn); self.conn.commit()
        self.assertEqual(tuple(self.conn.execute('SELECT merchant,category FROM transactions WHERE id=?', (future,)).fetchone()), ('Tiny Café', 'Eating out'))
        reopened = init_db(self.path)
        self.assertIsNotNone(reopened.execute('SELECT 1 FROM merchant_alias_approvals').fetchone())
        reopened.close()

    def test_learning_only_uses_same_label_saved_reviews_and_reports_conflicting_categories(self):
        ids = [self.add() for _ in range(4)]
        unrelated = self.add('Other Cafe')
        for ident, category in zip(ids[:3], ['Eating out', 'Eating out', 'Shopping']):
            self.client.post(f'/api/transactions/{ident}/review', json={'category': category})
        self.client.post(f'/api/transactions/{unrelated}/review', json={'category': 'Travel'})
        before = self.conn.execute('SELECT category FROM transactions WHERE id=?', (ids[-1],)).fetchone()[0]
        suggestion = self.preview(self.draft(ids[-1]))['learned_suggestion']
        self.assertEqual((suggestion['category'], suggestion['support'], suggestion['reviewed'], suggestion['conflicting']), ('Eating out', 2, 3, 1))
        self.assertEqual(self.conn.execute('SELECT category FROM transactions WHERE id=?', (ids[-1],)).fetchone()[0], before)

    def test_stale_preview_and_repeat_submission_never_duplicate_a_name(self):
        ident = self.add()
        draft = self.draft(ident)
        preview = self.preview(draft)
        self.client.post(f'/api/transactions/{ident}/review', json={'note': 'Changed in another tab'})
        stale = self.client.post('/api/merchants/classify', json={**draft, 'preview_token': preview['preview_token']})
        self.assertEqual(stale.status_code, 409)
        preview = self.preview(draft)
        payload = {**draft, 'preview_token': preview['preview_token']}
        self.assertEqual(self.client.post('/api/merchants/classify', json=payload).status_code, 200)
        self.assertEqual(self.client.post('/api/merchants/classify', json=payload).status_code, 409)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM merchants WHERE canonical_name='Tiny Café'").fetchone()[0], 1)

    def test_assign_existing_appends_alias_preserving_other_rules_and_null_category(self):
        ident = self.add('Tiny Cafe second label')
        rule = self.conn.execute("INSERT INTO merchants(canonical_name,kind,category,source,notes) VALUES('Existing cafe','merchant',NULL,'public_research','Keep research')").lastrowid
        original = self.conn.execute("INSERT INTO merchant_aliases(merchant_id,pattern) VALUES(?,'Original cafe')", (rule,)).lastrowid
        self.conn.commit()
        result = self.save(self.draft(ident, merchant_id=rule))
        self.assertIsNone(result['category'])
        self.assertEqual(tuple(self.conn.execute('SELECT notes,source FROM merchants WHERE id=?', (rule,)).fetchone()), ('Keep research', 'public_research'))
        self.assertIsNotNone(self.conn.execute('SELECT 1 FROM merchant_aliases WHERE id=?', (original,)).fetchone())

    def test_person_match_uses_full_transfer_description_and_keeps_friend_payments_in_spending(self):
        ident = self.add('Cleo', 'bank', 'To Cleo Example', type_code='BP')
        other = self.add('Cleo', 'bank', 'To Cleo Different', type_code='BP')
        card = self.add('Cleo')
        result = self.save(self.draft(ident, kind='person', canonical_name='Cleo', category='Friends & shared costs'))
        self.assertEqual((result['pattern'], result['matched_transactions']), ('cleo example', 1))
        self.assertEqual(tuple(self.conn.execute('SELECT category,spending_treatment FROM transactions WHERE id=?', (ident,)).fetchone()), ('Friends & shared costs', 'normal'))
        for row_id in (other, card):
            self.assertNotEqual(self.conn.execute('SELECT merchant_kind FROM transactions WHERE id=?', (row_id,)).fetchone()[0], 'person')
        bad = self.client.post('/api/merchants/classify/preview', json=self.draft(card, kind='person'))
        self.assertEqual(bad.status_code, 422)

    def test_missing_invalid_and_unsupported_self_transfer_requests_fail_without_writes(self):
        ident = self.add()
        self.assertEqual(self.client.post('/api/merchants/classify/preview', json=self.draft(999999)).status_code, 404)
        self.assertEqual(self.client.post('/api/merchants/classify/preview', json=self.draft(ident, category='!')).status_code, 422)
        self.assertEqual(self.client.post('/api/merchants/classify/preview', json=self.draft(ident, kind='self')).status_code, 422)
        self.assertEqual(self.client.post('/api/merchants/classify/preview', json=self.draft(ident, kind='self', spending_treatment='self_transfer')).status_code, 422)
        self.assertEqual(self.client.post('/api/merchants/classify', json=self.draft(ident)).status_code, 409)

    def test_untyped_friend_payment_uses_full_description_and_keeps_saved_reviews(self):
        from app.analytics import analytics_bundle

        description = 'Alex Morgan Shared dinner'
        ident = self.add('Alex', account='bank', description=description, amount=-100, type_code='bank_feed')
        reviewed = self.add('Alex', account='bank', description=description, amount=-20)
        self.client.post(f'/api/transactions/{reviewed}/review', json={
            'category': 'Shopping', 'note': 'Keep this individual purpose'})
        draft = self.draft(ident, canonical_name='Alex Morgan', kind='person', category='Friends & shared costs')
        before = [dict(row) for row in self.conn.execute('SELECT * FROM transactions ORDER BY id')]
        preview = self.preview(draft)
        self.assertEqual((preview['pattern'], preview['context'], preview['matched_transactions']),
                         (description, 'spending_bank', 2))
        self.assertFalse(preview['own_transfer_confirmation_required'])
        self.assertEqual(preview['similar_names'], [])
        self.assertEqual([dict(row) for row in self.conn.execute('SELECT * FROM transactions ORDER BY id')], before)
        result = self.save(draft)
        self.assertEqual(result['preserved_reviews'], 1)
        self.assertEqual(tuple(self.conn.execute('SELECT merchant,merchant_kind,category,spending_treatment FROM transactions WHERE id=?', (ident,)).fetchone()),
                         ('Alex Morgan', 'person', 'Friends & shared costs', 'normal'))
        self.assertEqual(self.conn.execute('SELECT category FROM transactions WHERE id=?', (reviewed,)).fetchone()[0], 'Shopping')
        self.assertEqual(self.conn.execute('SELECT note FROM overrides WHERE dedupe_key=?', (f'row:{reviewed}',)).fetchone()[0], 'Keep this individual purpose')
        fields = 'id,account_id,txn_date,description,source_merchant,amount,raw,meta,dedupe_key'
        self.assertEqual([tuple(row) for row in self.conn.execute(f'SELECT {fields} FROM transactions ORDER BY id')],
                         [tuple(row[name] for name in fields.split(',')) for row in before])
        self.assertEqual(analytics_bundle(self.conn)['stats']['total_spend'], 120)

    def test_untyped_friend_rule_matches_only_exact_bank_descriptions_on_future_imports(self):
        description = 'Alex Morgan Shared dinner'
        ident = self.add('Alex', account='bank', description=description)
        self.save(self.draft(ident, canonical_name='Alex Morgan', kind='person', category='Friends & shared costs'))
        future = [self.add('Alex', account='bank', description=description, type_code=code)
                  for code in (None, 'bank_feed')]
        unrelated = [
            self.add('Alex', account='bank', description='Alex Morgan Shared lunch'),
            self.add(description, account='bank', description='Other full bank description'),
            self.add('Alex', account='bank', description='To ' + description, type_code='BP'),
            self.add('Alex', account='card', description=description),
            self.add('Alex', account='broker', description=description),
            *[self.add('Alex', account='bank', description=description, type_code=code)
              for code in ('VIS', 'DEB', 'CARD', 'POS', 'ATM', 'CASH')],
        ]
        apply_categorization(self.conn); self.conn.commit()
        for row_id in future:
            self.assertEqual(self.conn.execute('SELECT merchant_kind FROM transactions WHERE id=?', (row_id,)).fetchone()[0], 'person')
        for other in unrelated:
            self.assertNotEqual(self.conn.execute('SELECT merchant_kind FROM transactions WHERE id=?', (other,)).fetchone()[0], 'person')

    def test_untyped_friend_can_reuse_shared_cost_rule_or_choose_transfer_category(self):
        from app.analytics import analytics_bundle

        typed = self.add('Alex', account='bank', description='To Alex Morgan', type_code='BP', amount=-100)
        friend = self.save(self.draft(typed, canonical_name='Alex Morgan', kind='person',
                                      category='Friends & shared costs', spending_treatment='reimbursement'))
        untyped = self.add('Alex', account='bank', description='Alex Morgan Shared dinner', amount=30)
        attached = self.save(self.draft(untyped, merchant_id=friend['merchant_id']))
        self.assertEqual((attached['context'], attached['spending_treatment']), ('spending_bank', 'reimbursement'))
        self.assertEqual(analytics_bundle(self.conn)['stats']['total_spend'], 70)
        other = self.add('Casey', account='bank', description='Casey Reed Shared dinner', amount=-100)
        result = self.save(self.draft(other, canonical_name='Casey Reed', kind='person', category='Transfers & payments'))
        self.assertEqual((result['category'], result['spending_treatment']), ('Transfers & payments', 'normal'))

    def test_untyped_friend_does_not_suggest_or_batch_other_descriptions(self):
        first = self.add('Alex', account='bank', description='Alex Morgan Shared dinner')
        other = self.add('Alex', account='bank', description='Alex Morgan Shared diner')
        draft = self.draft(first, canonical_name='Alex Morgan', kind='person')
        self.assertEqual(self.preview(draft)['similar_names'], [])
        response = self.client.post('/api/merchants/classify/preview', json={**draft, 'additional_example_ids': [other]})
        self.assertEqual(response.status_code, 409, response.text)

    def test_friend_rules_still_require_individual_review_for_card_and_cash_rows(self):
        for account, code in [('card', None), ('bank', 'VIS'), ('bank', 'DEB'), ('bank', 'CARD'),
                              ('bank', 'POS'), ('bank', 'ATM'), ('bank', 'CASH')]:
            with self.subTest(account=account, code=code):
                ident = self.add('Alex', account=account, description='Alex Morgan Shared dinner', type_code=code)
                response = self.client.post('/api/merchants/classify/preview', json=self.draft(ident, kind='person'))
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM merchants WHERE kind='person'").fetchone()[0], 0)

    def test_untyped_own_account_label_previews_then_requires_explicit_confirmation(self):
        description = 'Credit Bond 40123456'
        ident = self.add('Credit Bond', account='bank', description=description)
        reviewed = self.add('Credit Bond', account='bank', description=description)
        self.client.post(f'/api/transactions/{reviewed}/review', json={
            'spending_treatment': 'normal', 'category': 'One-off expense', 'note': 'Keep this exception'})
        own = self.conn.execute("INSERT INTO merchants(canonical_name,kind,category,spending_treatment,source) VALUES('Jordan · own account','self','Transfers & payments','self_transfer','user')").lastrowid
        self.conn.execute("INSERT INTO merchant_aliases(merchant_id,pattern,context) VALUES(?,'Jordan Example','transfer')", (own,))
        self.conn.commit()
        draft = self.draft(ident, merchant_id=own, confirm_own_transfer=False)
        before = [tuple(row) for row in self.conn.execute('SELECT * FROM transactions ORDER BY id')]
        preview = self.preview(draft)
        self.assertEqual((preview['pattern'], preview['context'], preview['matched_transactions']), (description, 'spending_bank', 2))
        self.assertTrue(preview['own_transfer_confirmation_required'])
        self.assertEqual(preview['similar_names'], [])
        self.assertEqual([tuple(row) for row in self.conn.execute('SELECT * FROM transactions ORDER BY id')], before)
        rejected = self.client.post('/api/merchants/classify', json={**draft, 'preview_token': preview['preview_token']})
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertIn('Confirm', rejected.json()['detail'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM merchant_aliases WHERE merchant_id=?', (own,)).fetchone()[0], 1)
        confirmed = {**draft, 'confirm_own_transfer': True}
        stale = self.client.post('/api/merchants/classify', json={**confirmed, 'preview_token': preview['preview_token']})
        self.assertEqual(stale.status_code, 409)
        result = self.save(confirmed)
        self.assertEqual((result['merchant_id'], result['preserved_reviews']), (own, 1))
        self.assertEqual(self.conn.execute('SELECT spending_treatment FROM transactions WHERE id=?', (ident,)).fetchone()[0], 'self_transfer')
        self.assertEqual(tuple(self.conn.execute('SELECT spending_treatment,category FROM transactions WHERE id=?', (reviewed,)).fetchone()), ('normal', 'One-off expense'))
        self.assertEqual(self.conn.execute('SELECT note FROM overrides WHERE dedupe_key=?', (f'row:{reviewed}',)).fetchone()[0], 'Keep this exception')

    def test_old_transfer_form_gets_reload_message_while_current_form_keeps_confirmation_checks(self):
        ident = self.add('Credit Bond', account='bank', description='Credit Bond 40123456')
        legacy = self.draft(ident, canonical_name='My savings', category='Transfers & payments',
                            kind='self', spending_treatment='self_transfer')
        before = [tuple(row) for row in self.conn.execute('SELECT * FROM transactions ORDER BY id')]
        aliases_before = self.conn.execute('SELECT COUNT(*) FROM merchant_aliases').fetchone()[0]
        preview = self.preview(legacy)
        self.assertTrue(preview['own_transfer_confirmation_required'])
        stale = self.client.post('/api/merchants/classify', json={**legacy, 'preview_token': '0' * 64})
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertIn('refreshed preview', stale.json()['detail'])
        outdated = self.client.post('/api/merchants/classify', json={**legacy, 'preview_token': preview['preview_token']})
        self.assertEqual(outdated.status_code, 409, outdated.text)
        self.assertIn('Reload the page, reopen this payment', outdated.json()['detail'])

        current = {**legacy, 'confirm_own_transfer': False}
        current_preview = self.preview(current)
        unconfirmed = self.client.post('/api/merchants/classify', json={**current, 'preview_token': current_preview['preview_token']})
        self.assertEqual(unconfirmed.status_code, 422, unconfirmed.text)
        self.assertIn('Confirm that this exact bank statement description', unconfirmed.json()['detail'])
        self.assertNotIn('Reload', unconfirmed.json()['detail'])
        confirmed = {**current, 'confirm_own_transfer': True}
        stale = self.client.post('/api/merchants/classify', json={**confirmed, 'preview_token': current_preview['preview_token']})
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertIn('refreshed preview', stale.json()['detail'])
        self.assertEqual([tuple(row) for row in self.conn.execute('SELECT * FROM transactions ORDER BY id')], before)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM merchant_aliases').fetchone()[0], aliases_before)
        self.assertEqual(self.save(confirmed)['spending_treatment'], 'self_transfer')

    def test_legacy_forms_can_still_save_rules_with_transfer_evidence_and_normal_merchants(self):
        transfer = self.add('My savings', account='bank', description='Payment to My savings', type_code='BP')
        drafts = [self.draft(transfer, canonical_name='My savings', category='Transfers & payments',
                             kind='self', spending_treatment='self_transfer'), self.draft(self.add())]
        for draft in drafts:
            with self.subTest(treatment=draft['spending_treatment']):
                self.assertNotIn('confirm_own_transfer', draft)
                self.assertFalse(self.preview(draft)['own_transfer_confirmation_required'])
                self.assertEqual(self.save(draft)['spending_treatment'], draft['spending_treatment'])

    def test_confirmed_own_transfer_matches_only_full_bank_descriptions_in_future_imports(self):
        description = 'Credit Bond 40123456'
        ident = self.add('Credit Bond', account='bank', description=description)
        result = self.save(self.draft(ident, canonical_name='My savings', category='Transfers & payments',
                                     kind='self', spending_treatment='self_transfer', confirm_own_transfer=True))
        future = self.add('Credit Bond', account='bank', description=description)
        unrelated = [
            self.add('Credit Bond', account='bank', description='Credit Bond 40987654'),
            self.add(description, account='bank', description='Different full bank description'),
            self.add(description, account='bank', description='To ' + description, type_code='BP'),
            self.add(description, account='card', description=description),
            self.add(description, account='broker', description=description),
            *[self.add(description, account='bank', description=description, type_code=code)
              for code in ('VIS', 'DEB', 'CARD', 'POS', 'ATM', 'CASH')],
        ]
        apply_categorization(self.conn); self.conn.commit()
        self.assertEqual(self.conn.execute('SELECT spending_treatment FROM transactions WHERE id=?', (future,)).fetchone()[0], 'self_transfer')
        for other in unrelated:
            self.assertEqual(self.conn.execute('SELECT spending_treatment FROM transactions WHERE id=?', (other,)).fetchone()[0], 'normal')
        # The new scope remains editable in the saved-name directory.
        rule = next(item for item in self.client.get('/api/merchants').json()['items'] if item['id'] == result['merchant_id'])
        response = self.client.put(f'/api/merchants/{rule["id"]}', json={**rule, 'expected_revision': rule['revision']})
        self.assertEqual(response.status_code, 200, response.text)
        rule['aliases'][0]['match_mode'] = 'prefix'
        response = self.client.put(f'/api/merchants/{rule["id"]}', json=rule)
        self.assertEqual(response.status_code, 422, response.text)

    def test_manual_transfer_confirmation_cannot_be_reused_for_similar_labels(self):
        first = self.add('Jordan Bond 40123456', account='bank', description='Jordan Bond 40123456')
        other = self.add('Jordan Bond 40987654', account='bank', description='Jordan Bond 40987654')
        draft = self.draft(first, canonical_name='Jordan Bond', kind='self', spending_treatment='self_transfer', confirm_own_transfer=True)
        self.assertEqual(self.preview(draft)['similar_names'], [])
        response = self.client.post('/api/merchants/classify/preview', json={**draft, 'additional_example_ids': [other]})
        self.assertEqual(response.status_code, 409, response.text)

    def test_manual_confirmation_cannot_turn_card_or_cash_rows_into_reusable_own_transfers(self):
        for account, code in [('card', None), ('bank', 'CARD'), ('bank', 'POS'), ('bank', 'ATM'), ('bank', 'CASH')]:
            with self.subTest(account=account, code=code):
                ident = self.add(account=account, type_code=code)
                draft = self.draft(ident, kind='self', spending_treatment='self_transfer', confirm_own_transfer=True)
                response = self.client.post('/api/merchants/classify/preview', json=draft)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIn('individually', response.json()['detail'])

    def test_concurrent_creation_from_one_preview_creates_one_rule(self):
        ident = self.add()
        draft = self.draft(ident)
        preview = self.preview(draft)
        payload = {**draft, 'preview_token': preview['preview_token']}
        with ThreadPoolExecutor(max_workers=2) as pool:
            codes = list(pool.map(lambda _: self.client.post('/api/merchants/classify', json=payload).status_code, range(2)))
        self.assertEqual(sorted(codes), [200, 409])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM merchants WHERE canonical_name='Tiny Café'").fetchone()[0], 1)

    def test_existing_card_alias_keeps_matching_broker_card_spending(self):
        ident = self.add('Existing Coffee', account='broker', type_code='CARD')
        rule = self.conn.execute("INSERT INTO merchants(canonical_name,kind,category) VALUES('Existing coffee shop','merchant','Eating out')").lastrowid
        self.conn.execute("INSERT INTO merchant_aliases(merchant_id,pattern,context) VALUES(?,'Existing Coffee','card')", (rule,))
        apply_categorization(self.conn); self.conn.commit()
        self.assertEqual(tuple(self.conn.execute('SELECT merchant,category FROM transactions WHERE id=?', (ident,)).fetchone()), ('Existing coffee shop', 'Eating out'))

    def test_funding_and_transfer_context_overlap_reports_conflict_before_saving(self):
        self.conn.execute("UPDATE accounts SET bank='Trading 212' WHERE id='broker'")
        ident = self.add('Example', account='bank', description='To Example', type_code='BP')
        rule = self.conn.execute("INSERT INTO merchants(canonical_name,kind,category,source) VALUES('Prior funding identity','merchant','Shopping','user')").lastrowid
        self.conn.execute("INSERT INTO merchant_aliases(merchant_id,pattern,context) VALUES(?,'example','funding')", (rule,))
        apply_categorization(self.conn); self.conn.commit()
        count = self.conn.execute('SELECT COUNT(*) FROM merchants').fetchone()[0]
        draft = self.draft(ident)
        preview = self.preview(draft)
        self.assertEqual(preview['conflict'], {'merchant_id': rule, 'canonical_name': 'Prior funding identity'})
        response = self.client.post('/api/merchants/classify', json={**draft, 'preview_token': preview['preview_token']})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM merchants').fetchone()[0], count)
        self.assertEqual(tuple(self.conn.execute('SELECT merchant,category FROM transactions WHERE id=?', (ident,)).fetchone()), ('Prior funding identity', 'Shopping'))
        # Choosing the existing identity intentionally keeps its definition.
        chosen = self.preview(self.draft(ident, merchant_id=rule))
        self.assertIsNone(chosen['conflict'])
        self.assertEqual((chosen['canonical_name'], chosen['category']), ('Prior funding identity', 'Shopping'))

    def test_funding_alias_does_not_conflict_when_account_evidence_is_absent(self):
        ident = self.add('Example', account='bank', description='To Example', type_code='BP')
        rule = self.conn.execute("INSERT INTO merchants(canonical_name,kind,category,source) VALUES('Dormant funding rule','merchant','Shopping','user')").lastrowid
        self.conn.execute("INSERT INTO merchant_aliases(merchant_id,pattern,context) VALUES(?,'example','funding')", (rule,))
        self.conn.commit()
        preview = self.preview(self.draft(ident))
        self.assertIsNone(preview['conflict'])


    def test_immediate_suggestions_match_spelling_variants_not_shared_processors_or_unrelated_names(self):
        first = self.add('Tiny Cafe')
        variant = self.add('Sumup Tiny Caffe')
        other = self.add('Other Cafe')
        numeric = self.add('Payment Ref 129999')
        processor = self.add('Sumup Other Shop')
        broker = self.add('Tiny Caffe', account='broker')
        result = self.preview(self.draft(first))
        self.assertEqual([item['example_id'] for item in result['similar_names']], [variant])
        suggestion = result['similar_names'][0]
        self.assertEqual((suggestion['context'], suggestion['n_transactions'], suggestion['outgoing_total']), ('spending_card', 1, 12))
        self.assertGreater(suggestion['score'], .8)
        self.assertEqual(result['additional_aliases'], [])
        self.assertEqual(result['matched_transactions'], 1)
        self.assertEqual(result['alias_count'], 1)
        self.assertNotIn(other, [item['example_id'] for item in result['similar_names']])

    def test_selected_variants_save_as_one_atomic_batch_and_preserve_reviews_and_future_imports(self):
        first = self.add('Tiny Cafe')
        chosen = self.add('Tiny Caffe')
        unchosen = self.add('Sumup Tiny Cafee')
        self.client.post(f'/api/transactions/{chosen}/review', json={'category': 'Client dinner', 'note': 'Keep this review'})
        draft = self.draft(first, additional_example_ids=[chosen])
        preview = self.preview(draft)
        self.assertEqual((preview['alias_count'], preview['matched_transactions'], preview['outgoing_total'], preview['preserved_reviews']), (2, 2, 24, 1))
        self.assertEqual([item['example_id'] for item in preview['additional_aliases']], [chosen])
        result = self.save(draft)
        aliases = [tuple(row) for row in self.conn.execute('SELECT pattern,match_mode,context FROM merchant_aliases WHERE merchant_id=? ORDER BY id', (result['merchant_id'],))]
        self.assertEqual(aliases, [('Tiny Cafe', 'exact', 'spending_card'), ('Tiny Caffe', 'exact', 'spending_card')])
        self.assertEqual(tuple(self.conn.execute('SELECT merchant,category FROM transactions WHERE id=?', (chosen,)).fetchone()), ('Tiny Café', 'Client dinner'))
        self.assertEqual(self.conn.execute('SELECT merchant FROM transactions WHERE id=?', (unchosen,)).fetchone()[0], 'Sumup Tiny Cafee')
        future = self.add('TINY CAFFE')
        apply_categorization(self.conn); self.conn.commit()
        self.assertEqual(tuple(self.conn.execute('SELECT merchant,category FROM transactions WHERE id=?', (future,)).fetchone()), ('Tiny Café', 'Eating out'))
        self.assertEqual(self.conn.execute('SELECT note FROM overrides WHERE dedupe_key=(SELECT dedupe_key FROM transactions WHERE id=?)', (chosen,)).fetchone()[0], 'Keep this review')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM merchant_alias_approvals WHERE alias_id IN (SELECT id FROM merchant_aliases WHERE merchant_id=?)', (result['merchant_id'],)).fetchone()[0], 2)

    def test_batch_runs_categorization_once_and_rolls_back_all_aliases_on_failure(self):
        from app import merchant_inbox, analytics
        first = self.add('Tiny Cafe')
        variant = self.add('Tiny Caffe')
        draft = merchant_inbox.QuickClassification(**self.draft(first, additional_example_ids=[variant]))
        prepared = merchant_inbox.preview(self.conn, draft)
        draft.preview_token = prepared['preview_token']
        names_before = self.conn.execute('SELECT COUNT(*) FROM merchants').fetchone()[0]
        aliases_before = self.conn.execute('SELECT COUNT(*) FROM merchant_aliases').fetchone()[0]
        with patch('app.analytics.recompute_all', side_effect=RuntimeError('Simulated analysis failure')):
            with self.assertRaisesRegex(RuntimeError, 'Simulated'):
                merchant_inbox.save(self.conn, draft)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM merchants').fetchone()[0], names_before)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM merchant_aliases').fetchone()[0], aliases_before)
        self.assertEqual(self.conn.execute('SELECT category FROM transactions WHERE id=?', (first,)).fetchone()[0], 'Uncategorized')
        with patch('app.categorize.apply_categorization', wraps=apply_categorization) as categorize, patch('app.analytics.recompute_all', wraps=analytics.recompute_all) as recompute:
            result = merchant_inbox.save(self.conn, draft)
            self.assertEqual(result['alias_count'], 2)
            categorize.assert_called_once()
            recompute.assert_called_once()


    def test_person_variants_use_complete_transfer_names_and_never_card_or_another_surname(self):
        first = self.add('Georgia', account='bank', description='To Georgia Example', type_code='BP')
        variant = self.add('Giorgia', account='bank', description='To Giorgia Example', type_code='BP')
        different = self.add('Georgia', account='bank', description='To Georgia Different', type_code='BP')
        short = self.add('Georgia', account='bank', description='To Georgia', type_code='BP')
        card = self.add('Giorgia Example')
        result = self.preview(self.draft(first, canonical_name='Georgia', kind='person', category='Friends & shared costs'))
        self.assertEqual([item['example_id'] for item in result['similar_names']], [variant])
        self.assertEqual(result['similar_names'][0]['context'], 'spending_transfer')

    def test_person_first_name_does_not_offer_unrelated_full_name(self):
        first = self.add('Cleo', account='bank', description='To Cleo', type_code='BP')
        self.add('Cleo', account='bank', description='To Cleo Different', type_code='BP')
        self.assertEqual(self.preview(self.draft(first, canonical_name='Cleo', kind='person'))['similar_names'], [])

    def test_own_transfer_suggestions_require_transfer_evidence(self):
        first = self.add('Jordan Example', account='bank', description='To Jordan Example', type_code='BP')
        variant = self.add('Jordon Example', account='bank', description='To Jordon Example', type_code='BP')
        self.add('Jordon Example')
        draft = self.draft(first, kind='self', spending_treatment='self_transfer', canonical_name='Jordan Example', category='Transfers & payments')
        self.assertEqual([item['example_id'] for item in self.preview(draft)['similar_names']], [variant])

    def test_common_processor_generic_and_reference_tokens_are_not_identity_evidence(self):
        from app.merchant_similarity import spelling_similarity
        for left, right in [('Sumup Evermere', 'Evermere Housing'), ('Payment Reference 123456', 'Payment Reference 123457'),
                            ('Paypal Coffee Shop', 'Sumup Coffee Shop'), ('Cleo Example', 'Cleo Different'),
                            ('Trading 212', 'Trading 213'), ('Sumup Somewhere', 'Sumup Somewhereelse')]:
            self.assertIsNone(spelling_similarity(left, right), (left, right))
        self.assertIsNotNone(spelling_similarity('Georgia Example', 'Giorgia Example', personal=True))
        self.assertIsNotNone(spelling_similarity('Blue Lantern Cafe', 'Sumup Blue Lantern Cafe'))

    def test_business_reference_and_store_suffix_variants_keep_distinct_services_and_people_separate(self):
        from app.merchant_similarity import spelling_similarity
        for left, right in [('Amazon Qh3Cb6315', 'Amazon Xh7Dp6316'), ('Amazon', 'Amazon Qh3Cb6315'),
                            ('Tesco', 'Tesco Stores London'), ('Tesco Stores', 'Tesco')]:
            match = spelling_similarity(left, right)
            self.assertIsNotNone(match, (left, right))
            self.assertIn('ignored', match[1])
        for left, right in [('Apple Music', 'Apple Store'), ('Evermere', 'Evermere Housing'),
                            ('Trading 212', 'Trading 213'), ('Tesla Model3', 'Tesla Model4'),
                            ('Amazon Prime', 'Amazon'), ('Payment Ref Ab12345', 'Payment Ref Ac12346')]:
            self.assertIsNone(spelling_similarity(left, right), (left, right))
        self.assertIsNone(spelling_similarity('Georgia Example Ab12345', 'Georgia Example Ac12346', personal=True))
        self.assertIsNone(spelling_similarity('Jordan B', 'Jordan C', personal=True))
        self.assertIsNone(spelling_similarity('Jordan Co', 'Jordan Ltd', personal=True))
        self.assertIsNone(spelling_similarity('John B', 'John C'))
        self.assertIsNotNone(spelling_similarity('Jordan B', 'Jordon B', personal=True))


    def test_dismissed_and_other_saved_identity_candidates_are_not_offered(self):
        first = self.add('Tiny Cafe')
        dismissed = self.add('Tiny Caffe')
        owned = self.add('Tiny Cafee')
        target = self.conn.execute("INSERT INTO merchants(canonical_name,kind,category) VALUES('Tiny Café','merchant','Eating out')").lastrowid
        other = self.conn.execute("INSERT INTO merchants(canonical_name,kind,category) VALUES('Different identity','merchant','Shopping')").lastrowid
        self.conn.execute("INSERT INTO merchant_aliases(merchant_id,pattern,match_mode,context) VALUES(?,'Tiny Cafee','prefix','spending')", (other,))
        self.conn.execute("INSERT INTO merchant_suggestion_decisions(label_key,merchant_id,source_label,status) VALUES('tiny caffe',?,'Tiny Caffe','dismissed')", (target,))
        self.conn.commit()
        # Also works before stored classifications have caught up with the rule.
        self.assertEqual(self.preview(self.draft(first, merchant_id=target))['similar_names'], [])

    def test_competing_exact_rule_on_another_descriptor_field_cannot_be_stolen(self):
        first = self.add('Tiny Cafe')
        candidate = self.add('Tiny Caffe', description='Completely separate saved descriptor')
        other = self.conn.execute("INSERT INTO merchants(canonical_name,kind,category) VALUES('Separate identity','merchant','Shopping')").lastrowid
        self.conn.execute("INSERT INTO merchant_aliases(merchant_id,pattern,context) VALUES(?,'Completely separate saved descriptor','spending_card')", (other,))
        self.conn.commit()
        self.assertEqual(self.preview(self.draft(first))['similar_names'], [])

    def test_arbitrary_duplicate_or_stale_candidate_selection_is_rejected_without_writes(self):
        first = self.add('Tiny Cafe')
        variant = self.add('Tiny Caffe')
        unrelated = self.add('Unrelated Shop')
        before = self.conn.execute('SELECT COUNT(*) FROM merchants').fetchone()[0]
        for ids in ([unrelated], [variant, variant], [first]):
            response = self.client.post('/api/merchants/classify/preview', json=self.draft(first, additional_example_ids=ids))
            self.assertEqual(response.status_code, 409, response.text)
        draft = self.draft(first, additional_example_ids=[variant])
        preview = self.preview(draft)
        self.client.post(f'/api/transactions/{variant}/review', json={'note': 'Edited while preview was open'})
        response = self.client.post('/api/merchants/classify', json={**draft, 'preview_token': preview['preview_token']})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM merchants').fetchone()[0], before)

    def test_selection_and_new_import_change_the_token(self):
        first = self.add('Tiny Cafe')
        variant = self.add('Tiny Caffe')
        original = self.preview(self.draft(first))
        draft = self.draft(first, additional_example_ids=[variant])
        selected = self.preview(draft)
        self.assertNotEqual(original['preview_token'], selected['preview_token'])
        response = self.client.post('/api/merchants/classify', json={**draft, 'preview_token': original['preview_token']})
        self.assertEqual(response.status_code, 409)
        self.add('Tiny Caffe')
        response = self.client.post('/api/merchants/classify', json={**draft, 'preview_token': selected['preview_token']})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM merchants WHERE canonical_name='Tiny Café'").fetchone()[0], 0)

    def test_read_only_finder_respects_directory_draft_kind_and_patterns(self):
        from app.merchant_inbox import SimilarNamesQuery, similar_names
        variant = self.add('Tiny Caffe')
        target = self.conn.execute("INSERT INTO merchants(canonical_name,kind,category) VALUES('Old name','person','Friends & shared costs')").lastrowid
        self.conn.commit()
        before = self.conn.total_changes
        body = SimilarNamesQuery(merchant_id=target, canonical_name='Tiny Cafe', kind='merchant', patterns=['Tiny Cafe'])
        result = similar_names(self.conn, body)
        self.assertEqual([item['example_id'] for item in result['items']], [variant])
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(similar_names(self.conn, SimilarNamesQuery(canonical_name='Ti'))['items'], [])


if __name__ == '__main__':
    unittest.main()

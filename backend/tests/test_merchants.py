import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from app import main, merchants, analytics
from app.db import init_db, apply_startup_migrations
from app.categorize import apply_categorization, auto_category, normalize_merchant
from app.ingest import _store_statement
from app.parsers.base import ParsedStatement, RawTxn
from app.parsers.revolut import _exchange_continuation


class MerchantTests(unittest.TestCase):
    def setUp(self):
        # Functional API fixtures use local mode; auth/security have dedicated suites.
        self.enterContext(patch.dict('os.environ', {'FINANCE_AUTH_ENABLED': 'false', 'FINANCE_DEPLOYMENT': 'local'}))
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'test.db'
        self.conn = init_db(self.path)
        self.conn.executemany('INSERT INTO accounts(id,bank,account_type,display_name) VALUES(?,?,?,?)', [
            ('a', 'Bank', 'current', 'Bank'), ('c', 'Amex', 'credit', 'Card'),
            ('i', 'Trading212', 'investment', 'Investments')])
        # Synthetic profiles belong to this test database, not shipped defaults.
        profiles = [
            ('Alex · own account', 'self', 'Transfers & payments', 'self_transfer', 'fixture-owner',
             ['Alex Morgan', 'Alex Jordan Morgan', 'Jamie Parker & Alex Morgan'], 'exact'),
            ('Jamie Parker', 'person', 'Friends & shared costs', 'normal', 'friend-jamie', ['Jamie Parker'], 'prefix'),
            ('Riley Brooks', 'person', 'Friends & shared costs', 'normal', 'friend-riley', ['Riley Brooks'], 'prefix'),
            ('Casey Bell', 'person', 'Friends & shared costs', 'normal', 'friend-casey', ['Casey Bell'], 'prefix'),
        ]
        for name, kind, category, treatment, seed, aliases, mode in profiles:
            cur = self.conn.execute("INSERT INTO merchants(canonical_name,kind,category,spending_treatment,source,seed_key) VALUES(?,?,?,?,'user_context',?)", (name, kind, category, treatment, seed))
            self.conn.executemany("INSERT INTO merchant_aliases(merchant_id,pattern,match_mode,context) VALUES(?,?,?,'transfer')", [(cur.lastrowid, alias, mode) for alias in aliases])
        self.conn.commit()
        self.patch = patch.object(main, 'DB_PATH', self.path)
        self.patch.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.patch.stop()
        self.conn.close()
        self.temp.cleanup()

    def add(self, description, amount=-20, account='a', code='BP', merchant=None, key=None):
        cur = self.conn.execute('INSERT INTO transactions(account_id,txn_date,description,merchant,amount,type_code,category,dedupe_key) VALUES(?,?,?,?,?,?,?,?)',
            (account, '2026-02-01', description, merchant or normalize_merchant(description), amount, code,
             auto_category(description, merchant or '', amount), key))
        self.conn.commit()
        return cur.lastrowid

    def txn(self, ident):
        return dict(self.conn.execute('SELECT * FROM transactions WHERE id=?', (ident,)).fetchone())

    def test_fresh_database_has_no_personal_defaults_and_existing_private_rules_survive_init(self):
        fresh = init_db(Path(self.temp.name) / 'empty-profile.db')
        self.assertEqual(fresh.execute("SELECT COUNT(*) FROM merchants WHERE kind IN ('person','self')").fetchone()[0], 0)
        fresh.close()
        before = [tuple(row) for row in self.conn.execute("SELECT id,canonical_name,kind,enabled FROM merchants WHERE source='user_context' ORDER BY id")]
        reopened = init_db(self.path)
        after = [tuple(row) for row in reopened.execute("SELECT id,canonical_name,kind,enabled FROM merchants WHERE source='user_context' ORDER BY id")]
        reopened.close()
        self.assertEqual(before, after)

    def test_observed_self_identities_and_joint_account_require_full_name_and_transfer_context(self):
        own = [self.add(d, amount) for d, amount in [
            ('ALEX MORGAN', -200), ('Payment from ALEX JORDAN MORGAN', 200),
            ('Transfer to JAMIE PARKER & ALEX MORGAN', -100)]]
        unrelated = [self.add('Robin Ellis Alex Jordan Morgan', 23),
                     self.add('Alex Fine Foods', -30),
                     self.add('ALEX MORGAN', -40, account='c')]
        apply_categorization(self.conn)
        for ident in own:
            self.assertEqual(self.txn(ident)['spending_treatment'], 'self_transfer')
        for ident in unrelated:
            self.assertEqual(self.txn(ident)['spending_treatment'], 'normal')

    def test_friends_remain_spending_not_transfers_or_refunds_without_review(self):
        ids = [self.add('Jamie Parker Deposit Refund'), self.add('Casey Bell Thingy'),
               self.add('Riley Brooks Deposit Refund'), self.add('Transfer from JAMIE PARKER', 50)]
        noise = self.add('Taylor Rivers JAMIE BDAY', 23)
        apply_categorization(self.conn)
        for ident in ids:
            t = self.txn(ident)
            self.assertEqual((t['merchant_kind'], t['category'], t['spending_treatment']),
                             ('person', 'Friends & shared costs', 'normal'))
        self.assertEqual(self.txn(noise)['merchant_kind'], 'unknown')

    def test_known_broker_funding_is_symmetric_but_card_purchase_and_opaque_payment_stay(self):
        yes = [self.add('Trading 212 London', -1000, code='VIS'), self.add('TRADING 212 UK G', 1200, code='FPI')]
        no = [self.add('Trading 212', -100, account='c'), self.add('Unknown transfer', -1000)]
        apply_categorization(self.conn)
        self.assertTrue(all(self.txn(i)['spending_treatment'] == 'self_transfer' for i in yes))
        self.assertTrue(all(self.txn(i)['spending_treatment'] == 'normal' for i in no))

    def test_merchant_rule_deletion_restores_source_and_seed_disable_survives_restart(self):
        ident = self.add('MYSTERY SHOP', merchant='Original Label', key='shop')
        body = {'canonical_name': 'A new cafe', 'category': 'Eating out',
                'aliases': [{'pattern': 'MYSTERY SHOP'}]}
        res = self.client.post('/api/merchants', json=body)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(self.txn(ident)['merchant'], 'A new cafe')
        self.client.delete('/api/merchants/' + str(res.json()['id']))
        self.assertEqual(self.txn(ident)['merchant'], 'Original Label')
        seed = next(m for m in merchants.list_merchants(self.conn) if m['seed_key'] == 'friend-casey')
        self.client.delete('/api/merchants/' + str(seed['id']))
        c = init_db(self.path); c.close()
        self.assertFalse(self.conn.execute('SELECT enabled FROM merchants WHERE id=?', (seed['id'],)).fetchone()[0])

    def test_fuzzy_match_is_a_suggestion_until_exact_alias_is_approved(self):
        ident = self.add('Cofee Corner', merchant='Cofee Corner', key='typo')
        res = self.client.post('/api/merchants', json={'canonical_name': 'Coffee Corner', 'category': 'Eating out',
            'aliases': [{'pattern': 'Coffee Corner'}]})
        self.assertEqual(res.status_code, 200)
        suggestion = next(s for s in self.client.get('/api/merchants').json()['suggestions'] if s['example_id'] == ident)
        self.assertEqual(suggestion['suggested_name'], 'Coffee Corner')
        self.assertEqual(self.txn(ident)['merchant'], 'Cofee Corner')
        self.assertEqual(self.txn(ident)['category'], 'Uncategorized')
        self.assertFalse(json.loads(self.txn(ident)['classification']).get('merchant_id'))

    def test_review_is_partial_persistent_and_resettable(self):
        ident = self.add('Casey Bell Thingy', key='casey')
        url = f'/api/transactions/{ident}/review'
        r = self.client.post(url, json={'category': 'Eating out', 'merchant': 'Dinner with Casey',
             'merchant_kind': 'person', 'spending_treatment': 'reimbursement', 'exclude_from_analysis': True, 'note': 'One-off shared meal'})
        self.assertEqual(r.status_code, 200, r.text)
        self.client.post(url, json={'note': 'Updated note only'})
        self.client.post('/api/recompute', json={})
        t = self.client.get(url).json()
        self.assertEqual(t['transaction']['merchant'], 'Dinner with Casey')
        self.assertTrue(t['transaction']['exclude_from_analysis'])
        self.assertEqual(t['review']['note'], 'Updated note only')
        self.client.post(url, json={'exclude_from_analysis': None})
        self.assertFalse(self.client.get(url).json()['transaction']['exclude_from_analysis'])
        reset = self.client.delete(url).json()
        self.assertEqual(reset['transaction']['merchant'], 'Casey Bell')
        self.assertEqual(reset['transaction']['spending_treatment'], 'normal')
        self.assertEqual(reset['transaction']['category'], 'Friends & shared costs')
        self.assertEqual(reset['review'], {})

    def test_manual_legacy_rows_without_import_keys_have_independent_reviews(self):
        a, b = self.add('First'), self.add('Second')
        self.client.post(f'/api/transactions/{a}/review', json={'category': 'First review'})
        self.client.post(f'/api/transactions/{b}/review', json={'category': 'Second review'})
        self.client.post('/api/recompute', json={})
        self.assertEqual(self.txn(a)['category'], 'First review')
        self.assertEqual(self.txn(b)['category'], 'Second review')

    def test_processor_labels_keep_merchant_and_category_boundaries(self):
        self.assertIn('Trip', normalize_merchant('000*TRIP.COM'))
        self.assertIn('Demo', normalize_merchant('SQ *DEMO DENTAL'))
        self.assertIn('Demoshop', normalize_merchant('SPO*DEMOSHOP'))
        self.assertNotEqual(auto_category('EXAMPLE WATERLOO', '', -10), 'Bills & utilities')
        self.assertNotEqual(auto_category('NESPRESSO', '', -10), 'Subscriptions')
        self.assertEqual(auto_category('VODAFONE', '', -10), 'Phone & internet')

    def test_reimport_and_legacy_dedupe_migration_preserve_all_review_fields(self):
        statement = ParsedStatement('Bank', 'current', 'Bank-1234', None, '1234',
            date(2026, 1, 1), date(2026, 1, 31), 100, 80,
            [RawTxn(date(2026, 1, 3), 'Shop', -20, merchant='Original shop')])
        _store_statement(self.conn, statement, 'one.pdf')
        self.conn.commit()
        ident = self.conn.execute("SELECT id FROM transactions WHERE description='Shop'").fetchone()[0]
        self.client.post(f'/api/transactions/{ident}/review', json={'category': 'Travel',
            'merchant': 'Reviewed label', 'merchant_kind': 'person', 'spending_treatment': 'reimbursement',
            'exclude_from_analysis': True, 'note': 'Keep after import'})
        self.conn.execute("UPDATE overrides SET dedupe_key='legacy-key'")
        self.conn.execute("UPDATE transactions SET dedupe_key='legacy-key' WHERE id=?", (ident,))
        self.conn.commit()
        self.assertEqual(_store_statement(self.conn, statement, 'one.pdf')['new_txns'], 0)
        apply_categorization(self.conn)
        self.conn.commit()
        result = self.client.get(f'/api/transactions/{ident}/review').json()
        self.assertEqual(result['transaction']['merchant'], 'Reviewed label')
        self.assertEqual(result['transaction']['source_merchant'], 'Original shop')
        self.assertEqual(result['review']['spending_treatment'], 'reimbursement')
        self.assertEqual(result['review']['merchant_kind'], 'person')
        self.assertTrue(result['review']['exclude_from_analysis'])
        self.assertEqual(result['review']['note'], 'Keep after import')

    def test_public_catalog_research_matches_original_processor_descriptor(self):
        ident = self.add('000*TRIP.COM', merchant='000 Com')
        apply_categorization(self.conn)
        row = self.txn(ident)
        self.assertEqual(row['merchant'], 'Trip.com')
        self.assertEqual(row['category'], 'Travel')
        explanation = json.loads(row['classification'])
        self.assertEqual(explanation['source'], 'public_research')
        self.assertTrue(explanation['source_url'].startswith('https://'))

    def test_explicit_normal_or_spending_category_overrides_seed_transfer_treatment(self):
        first = self.add('ALEX MORGAN', key='own-first')
        second = self.add('ALEX MORGAN', key='own-second')
        self.client.post(f'/api/transactions/{first}/review', json={'spending_treatment': 'normal'})
        self.client.post(f'/api/transactions/{second}/category', json={'category': 'Housing'})
        self.client.post('/api/recompute', json={})
        for ident in (first, second):
            row = self.txn(ident)
            self.assertEqual(row['spending_treatment'], 'normal')
            self.assertNotEqual(row['category'], 'Transfers & payments')
            self.assertIn(ident, [t['id'] for t in analytics.cashflow_rows(self.conn)])

    def test_unmatched_self_transfers_appear_in_transfer_view_and_filter_without_double_listing_pairs(self):
        self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('b','Other bank','current','Other bank')")
        first = self.add('ALEX MORGAN', -100, key='paired-out')
        second = self.add('ALEX MORGAN', 100, account='b', code='FPI', key='paired-in')
        third = self.add('ALEX MORGAN', -20, key='unpaired')
        self.client.post('/api/recompute', json={})
        data = self.client.get('/api/transfers').json()
        self.assertEqual(len([t for t in data if t['matched']]), 1)
        self.assertEqual([t['txn_id'] for t in data if not t['matched']], [third])
        self.assertEqual(self.client.get('/api/transactions?transfer=true').json()['total'], 3)
        self.assertEqual({t['txn_id'] for t in data}, {first, third})

    def test_startup_upgrade_classifies_existing_rows_once_and_new_direct_imports_trigger_again(self):
        ident = self.add('ALEX MORGAN', key='legacy-source')
        self.assertTrue(apply_startup_migrations(self.conn))
        self.assertEqual(self.txn(ident)['spending_treatment'], 'self_transfer')
        with patch('app.analytics.recompute_all') as recompute:
            self.assertFalse(apply_startup_migrations(self.conn))
            recompute.assert_not_called()
        second = self.add('Casey Bell Thingy', key='new-direct-import')
        self.assertTrue(apply_startup_migrations(self.conn))
        self.assertEqual(self.txn(second)['merchant_kind'], 'person')

    def test_editing_friend_prefix_does_not_override_exact_shared_account_identity(self):
        ident = self.add('Transfer to JAMIE PARKER & ALEX MORGAN', key='joint')
        friend = next(item for item in merchants.list_merchants(self.conn) if item['seed_key'] == 'friend-jamie')
        response = self.client.put(f"/api/merchants/{friend['id']}", json={
            'canonical_name': 'Jamie', 'kind': 'person', 'category': 'Friends & shared costs',
            'aliases': [{'pattern': 'JAMIE PARKER', 'match_mode': 'prefix', 'context': 'transfer'}]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.txn(ident)['spending_treatment'], 'self_transfer')

    def test_masked_own_card_reference_identifies_unmatched_repayment_conservatively(self):
        self.conn.execute("UPDATE accounts SET bank='HSBC',account_number='1234561111119876',display_name='HSBC Card' WHERE id='c'")
        yes = self.add('HSBC REWARDS123456 123456******9876', -1000, key='card-payment')
        wrong = self.add('HSBC REWARDS123456 123456******4321', -1000, key='wrong-card')
        shop = self.add('HSBC REWARDS123456 123456******9876', -50, account='c', code='DR', key='card-purchase')
        unknown = self.add('HSBC Branch Services', -30, key='generic-bank')
        apply_categorization(self.conn)
        self.assertEqual(self.txn(yes)['spending_treatment'], 'self_transfer')
        self.assertTrue(all(self.txn(i)['spending_treatment'] == 'normal' for i in (wrong, shop, unknown)))
        provenance = json.loads(self.txn(yes)['classification'])
        self.assertEqual(provenance['source'], 'account_evidence')
        self.assertIn('Masked payee card reference', provenance['explanation'])

    def test_obp_card_repayment_learns_exact_payee_from_unique_counterpart_and_respects_disable(self):
        self.conn.execute("UPDATE accounts SET bank='AMEX',account_number='xxxx-xxxxxx-11111' WHERE id='c'")
        desc = 'AMERICAN EXP 1234 PB5678******22222'
        first = self.add(desc, -119.18, code='OBP', key='bank-payment')
        self.add('PAYMENT RECEIVED - THANK YOU', 119.18, account='c', code='CR', key='card-receipt')
        generic = self.add('AMERICAN EXPRESS TRAVEL', -119.18, code='OBP', key='travel')
        apply_categorization(self.conn); self.conn.commit()
        self.assertEqual(self.txn(first)['spending_treatment'], 'self_transfer')
        self.assertEqual(self.txn(generic)['spending_treatment'], 'normal')
        later = self.add(desc, -250, code='OBP', key='later-without-receipt')
        apply_categorization(self.conn); self.conn.commit()
        self.assertEqual(self.txn(later)['spending_treatment'], 'self_transfer')
        rule = json.loads(self.txn(first)['classification'])['merchant_id']
        self.client.delete(f'/api/merchants/{rule}')
        apply_categorization(self.conn)
        self.assertEqual(self.txn(first)['spending_treatment'], 'normal')
        self.assertEqual(self.txn(later)['spending_treatment'], 'normal')

    def test_prefixed_masked_reference_matches_imported_card_without_counterpart_statement(self):
        self.conn.execute("UPDATE accounts SET bank='AMEX',account_number='xxxx-xxxxxx-12345' WHERE id='c'")
        correct = self.add('AMERICAN EXP 1234 PB5678******12345', -119, code='OBP', key='matching-reference')
        wrong = self.add('AMERICAN EXP 1234 PB5678******54321', -119, code='OBP', key='wrong-reference')
        generic = self.add('AMERICAN EXPRESS TRAVEL', -119, code='OBP', key='issuer-only')
        card = self.add('AMERICAN EXP 1234 PB5678******12345', -119, account='c', code='DR', key='card-purchase')
        apply_categorization(self.conn)
        self.assertEqual(self.txn(correct)['spending_treatment'], 'self_transfer')
        self.assertEqual(json.loads(self.txn(correct)['classification'])['source'], 'account_evidence')
        for ident in (wrong, generic, card):
            self.assertEqual(self.txn(ident)['spending_treatment'], 'normal')

    def test_cash_machine_location_is_not_the_grocery_category(self):
        ident = self.add('CASH SAINSBY Cashpoint Sainsburys', -100, code='ATM', key='cash')
        apply_categorization(self.conn)
        self.assertEqual(self.txn(ident)['category'], 'Cash')
        self.assertEqual(self.txn(ident)['spending_treatment'], 'normal')

    def test_currency_exchange_fee_metadata_reconciles_without_changing_source_amount(self):
        txn = RawTxn(date(2026, 1, 1), 'Exchanged to EUR', -84.42,
            meta={'currency_exchange': True, 'exchange_fee_gbp': 0, 'exchange_currency': 'EUR'})
        _exchange_continuation(txn, 'Fee: £0.84 £83.58')
        self.assertEqual(txn.amount, -84.42)
        self.assertEqual(txn.meta['exchange_fee_gbp'], .84)
        self.assertEqual(txn.meta['exchange_principal_gbp'], 83.58)
        with self.assertRaises(ValueError):
            _exchange_continuation(txn, 'Fee: £0.84 £70.00')

    def test_review_and_merchant_validation_rejects_invalid_choices(self):
        ident = self.add('Shop')
        self.assertEqual(self.client.post(f'/api/transactions/{ident}/review', json={'spending_treatment': 'hide'}).status_code, 422)
        self.assertEqual(self.client.post('/api/merchants', json={'canonical_name': 'Merchant', 'aliases': [{'pattern': '  '}]}).status_code, 422)
        self.assertEqual(self.client.post('/api/merchants', json={'canonical_name': 'Merchant', 'aliases': [{'pattern': 'x'}], 'source_url': 'javascript:alert(1)'}).status_code, 422)
        self.assertEqual(self.client.post('/api/merchants', json={'canonical_name': 'My account', 'kind': 'self', 'spending_treatment': 'normal', 'aliases': [{'pattern': 'Full name'}]}).status_code, 422)

    def test_preferences_persist_and_unknown_groups_are_rejected(self):
        payload = {'account': 'a', 'date_from': '2026-01-01', 'exclude_outliers': True}
        self.assertEqual(self.client.put('/api/preferences/spending', json=payload).status_code, 200)
        self.assertEqual(self.client.get('/api/preferences/spending').json()['value'], payload)
        self.assertEqual(self.client.put('/api/preferences/anything', json={}).status_code, 422)


if __name__ == '__main__':
    unittest.main()

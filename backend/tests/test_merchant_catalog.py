import json
import tempfile
import unittest
from pathlib import Path

from app.categorize import apply_categorization, normalize_merchant
from app.db import init_db
from app.merchants import _prepared, classify, seed_merchants


class PublicMerchantCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'catalog.db'
        self.conn = init_db(self.path)
        self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('card','Example','credit','Example card')")
        self.aliases = _prepared(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def match(self, description, account_type='current', type_code=None):
        return classify({'description': description, 'source_merchant': normalize_merchant(description),
                         'account_type': account_type, 'type_code': type_code, 'amount': -12}, self.aliases)

    def add(self, description):
        self.conn.execute("""INSERT INTO transactions(account_id,txn_date,description,merchant,
            amount,type_code,dedupe_key,raw,meta) VALUES('card','2026-09-01',?,?,-12,'DR','example',?,?)""",
            (description, normalize_merchant(description), 'Synthetic source row',
             json.dumps({'source_merchant': normalize_merchant(description)})))

    def test_general_business_names_keep_distinct_identities(self):
        for description, name, category in [('TESCO SAMPLE', 'Tesco', 'Groceries'),
                ('AMAZON SAMPLE', 'Amazon', 'Shopping'), ('NETFLIX', 'Netflix', 'Subscriptions'),
                ('SPOTIFY SAMPLE', 'Spotify', 'Subscriptions')]:
            with self.subTest(description=description):
                result = self.match(description)
                self.assertEqual((result['merchant'], result['category']), (name, category))
                self.assertEqual(result['spending_treatment'], 'normal')
                self.assertEqual(result['classification']['source'], 'public_research')
                self.assertTrue(result['classification']['source_url'].startswith('https://'))

    def test_original_processor_description_is_available_to_classifier(self):
        result = self.match('000*TRIP.COM')
        self.assertEqual((result['merchant'], result['category']), ('Trip.com', 'Travel'))
        self.assertEqual(result['classification']['matched_field'], 'description')
        self.assertEqual(self.match('IAP OPENAI SAMPLE')['merchant'], 'OpenAI')

    def test_short_ambiguous_and_unrelated_names_are_not_seeded(self):
        for description in ('NET', 'SPOT', 'TRIP', 'OPEN', 'EXAMPLE HOUSING', 'SAMPLE SHOP'):
            with self.subTest(description=description):
                self.assertIsNone(self.match(description, type_code='BP'))

    def test_investment_context_cannot_be_captured_as_shopping(self):
        for description in ('TESCO', 'AMAZON', 'NETFLIX', 'SPOTIFY', 'TRIP.COM', 'OPENAI'):
            with self.subTest(description=description):
                self.assertIsNone(self.match(description, account_type='investment'))

    def test_transaction_review_and_source_data_survive_reclassification(self):
        self.add('TESCO SAMPLE')
        self.conn.execute("""INSERT INTO overrides(dedupe_key,category,merchant,spending_treatment,
            exclude_from_analysis,note) VALUES('example','Eating out','Reviewed cafe meal',
            'reimbursement',1,'Synthetic shared meal')""")
        before = tuple(self.conn.execute('SELECT id,description,amount,raw,meta,dedupe_key FROM transactions').fetchone())
        apply_categorization(self.conn)
        result = dict(self.conn.execute('SELECT * FROM transactions').fetchone())
        self.assertEqual((result['merchant'], result['category']), ('Reviewed cafe meal', 'Eating out'))
        self.assertEqual((result['spending_treatment'], result['exclude_from_analysis']), ('reimbursement', 1))
        self.assertEqual(result['category_source'], 'user')
        self.assertEqual(before, tuple(self.conn.execute('SELECT id,description,amount,raw,meta,dedupe_key FROM transactions').fetchone()))
        self.assertEqual(apply_categorization(self.conn), 0)

    def test_disabled_and_edited_defaults_survive_restart(self):
        self.add('SPOTIFY SAMPLE')
        self.conn.execute("UPDATE merchants SET enabled=0 WHERE seed_key='public:spotify'")
        self.conn.execute("UPDATE merchants SET category='Eating out',notes='Synthetic reviewed category' WHERE seed_key='public:tesco'")
        self.conn.commit()
        reopened = init_db(self.path)
        reopened.close()
        seed_merchants(self.conn)
        aliases = _prepared(self.conn)
        self.assertIsNone(classify({'description': 'SPOTIFY SAMPLE', 'account_type': 'credit'}, aliases))
        edited = classify({'description': 'TESCO SAMPLE', 'account_type': 'credit'}, aliases)
        self.assertEqual(edited['category'], 'Eating out')
        self.assertIn('Synthetic reviewed category', edited['classification']['explanation'])

    def test_existing_user_identity_wins_over_public_default(self):
        self.add('TESCO')
        cur = self.conn.execute("""INSERT INTO merchants(canonical_name,kind,category,spending_treatment,source)
            VALUES('Reviewed groceries','merchant','Shared meals','normal','user')""")
        self.conn.execute("INSERT INTO merchant_aliases(merchant_id,pattern,match_mode,context) VALUES(?,'TESCO','exact','spending')", (cur.lastrowid,))
        apply_categorization(self.conn)
        row = self.conn.execute('SELECT merchant,category,classification FROM transactions').fetchone()
        self.assertEqual((row['merchant'], row['category']), ('Reviewed groceries', 'Shared meals'))
        self.assertEqual(json.loads(row['classification'])['source'], 'user')


if __name__ == '__main__':
    unittest.main()

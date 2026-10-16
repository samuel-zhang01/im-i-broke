"""Bank review actions remain usable without weakening source preservation."""
from pathlib import Path
import tempfile
import unittest

from app.db import init_db
from app import banking_health, lunchflow_client as client, lunchflow_store as store


class BankingReviewRefinementTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.conn = init_db(Path(folder.name) / 'bank.db')
        self.addCleanup(self.conn.close)
        store.ensure_schema(self.conn)
        self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('saved','Example Bank','current','Saved account')")
        self.conn.execute("INSERT INTO statements(id,account_id,source_file,period_start,period_end) VALUES(1,'saved','original.pdf','2026-08-01','2026-08-31')")
        self.conn.execute("""INSERT INTO transactions(id,account_id,statement_id,txn_date,description,merchant,
            amount,category,category_source,dedupe_key)
            VALUES(1,'saved',1,'2026-08-29','Coffee Shop','Coffee Shop',-12,'User category','user','original')""")
        accounts = client.list_accounts({'accounts': [{'id': 42, 'name': 'Current account',
            'institution_name': 'Example Bank', 'status': 'ACTIVE', 'currency': 'GBP'}]})
        store.discover_accounts(self.conn, accounts, '2026-09-19T10:00:00+00:00')
        store.map_account(self.conn, '42', account_id='saved')
        self.original = dict(self.conn.execute('SELECT * FROM transactions WHERE id=1').fetchone())

    def save(self, key='original', *, pending=False, amount=-12, currency='GBP', stamp='2026-09-19T10:01:00+00:00'):
        payload = {'id': key, 'accountId': 42, 'date': '2026-08-29', 'amount': amount,
                   'currency': currency, 'merchant': 'Coffee Shop', 'description': 'Coffee Shop', 'isPending': pending}
        store.record_account_result(self.conn, '42', {
            'balance': {'amount': 100, 'currency': 'GBP'},
            'transactions': client.transactions({'transactions': [payload]}, '42'), 'errors': []}, stamp)
        return self.conn.execute('SELECT * FROM bank_transactions WHERE provider_id=?', (key,)).fetchone()

    def test_linked_pending_revision_can_be_ignored_but_never_reimported(self):
        self.save()
        revised = self.save(pending=True, stamp='2026-09-19T11:01:00+00:00')
        self.assertEqual((revised['status'], revised['transaction_id'], revised['is_pending']), ('review', 1, 1))
        for action in ('match', 'import'):
            with self.subTest(action=action), self.assertRaises(ValueError):
                store.resolve_review(self.conn, revised['id'], action, 1)
        store.resolve_review(self.conn, revised['id'], 'ignore')
        repeated = self.save(pending=True, stamp='2026-09-19T12:01:00+00:00')
        self.assertEqual((repeated['status'], repeated['transaction_id']), ('ignored', 1))
        self.assertEqual(dict(self.conn.execute('SELECT * FROM transactions WHERE id=1').fetchone()), self.original)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT action FROM bank_review_decisions').fetchone()[0], 'ignore')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM bank_transaction_revisions').fetchone()[0], 1)

    def test_pending_authorization_still_cannot_be_reviewed_or_imported(self):
        pending = self.save(pending=True)
        for action in ('ignore', 'import', 'match'):
            with self.subTest(action=action), self.assertRaises(ValueError):
                store.resolve_review(self.conn, pending['id'], action, 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 1)

    def test_currency_revision_can_be_ignored_without_relabeling_saved_ledger(self):
        self.save()
        revised = self.save(currency='SGD', stamp='2026-09-19T11:01:00+00:00')
        self.assertEqual((revised['status'], revised['transaction_id']), ('review', 1))
        for action in ('match', 'import'):
            with self.subTest(action=action), self.assertRaises(ValueError):
                store.resolve_review(self.conn, revised['id'], action, 1)
        store.resolve_review(self.conn, revised['id'], 'ignore')
        self.assertEqual(dict(self.conn.execute('SELECT * FROM transactions WHERE id=1').fetchone()), self.original)
        self.assertEqual(self.conn.execute('SELECT currency FROM accounts WHERE id=\'saved\'').fetchone()[0], 'GBP')
        self.assertEqual(self.conn.execute('SELECT status FROM bank_transactions').fetchone()[0], 'ignored')

    def test_claimed_match_is_not_offered_but_still_prevents_duplicate_import(self):
        self.save()
        duplicate = self.save('second-source', stamp='2026-09-19T11:01:00+00:00')
        self.assertEqual(duplicate['status'], 'review')
        detail = store.detail(self.conn, '42', status='review')
        self.assertEqual(detail['total'], 1)
        self.assertEqual(detail['reviews'][0]['candidates'], [])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 1)
        # Another still-unclaimed statement occurrence remains selectable.
        self.conn.execute("""INSERT INTO transactions(id,account_id,statement_id,txn_date,description,merchant,amount,dedupe_key)
            VALUES(2,'saved',1,'2026-08-29','Second purchase','Second purchase',-12,'second')""")
        detail = store.detail(self.conn, '42', status='review')
        self.assertEqual([row['id'] for row in detail['reviews'][0]['candidates']], [2])
        store.resolve_review(self.conn, duplicate['id'], 'match', 2)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 2)

    def test_linked_revision_never_offers_another_statement_match(self):
        self.save()
        self.conn.execute("""INSERT INTO transactions(id,account_id,statement_id,txn_date,description,merchant,amount,dedupe_key)
            VALUES(2,'saved',1,'2026-08-29','Other purchase','Other purchase',-15,'other')""")
        self.save(amount=-15, stamp='2026-09-19T11:01:00+00:00')
        detail = store.detail(self.conn, '42', status='review')
        self.assertEqual(detail['reviews'][0]['transaction_id'], 1)
        self.assertEqual(detail['reviews'][0]['candidates'], [])
        self.assertEqual(dict(self.conn.execute('SELECT * FROM transactions WHERE id=1').fetchone()), self.original)

    def test_health_distinguishes_preserved_revisions_from_invalid_ledger_links(self):
        self.save()
        for revision, indicator in [({'pending': True}, 'pending_ledger_links'),
                                    ({'currency': 'SGD'}, 'foreign_currency_ledger_links')]:
            with self.subTest(revision=revision):
                revised = self.save(**revision, stamp='2026-09-19T11:01:00+00:00')
                self.assertEqual(revised['status'], 'review')
                self.assertEqual(banking_health.snapshot(self.conn)['integrity_issues'][indicator], 0)
                store.resolve_review(self.conn, revised['id'], 'ignore')
                self.assertEqual(banking_health.snapshot(self.conn)['integrity_issues'][indicator], 0)
                # Simulate invalid external mutation: actually accepting the
                # revised payload into the ledger must remain detectable.
                for invalid_status in ('imported', 'matched'):
                    self.conn.execute('UPDATE bank_transactions SET status=? WHERE id=?',
                                      (invalid_status, revised['id']))
                    self.assertEqual(banking_health.snapshot(self.conn)['integrity_issues'][indicator], 1)
        self.assertEqual(dict(self.conn.execute('SELECT * FROM transactions WHERE id=1').fetchone()), self.original)


if __name__ == '__main__':
    unittest.main()

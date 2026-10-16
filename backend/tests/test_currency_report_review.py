"""Independent native-report review regressions using synthetic evidence only."""
import unittest

from app import currency_reporting
from tests import test_workspace_product as fixture_module


class CurrencyReviewEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.WorkspaceProductTests('test_defaults_and_workspace_identity_survive_initialization')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.conn, self.client = self.fixture.conn, self.fixture.client
        self.ident = self.fixture.native_bank()

    def report(self):
        return currency_reporting.cashflow_report(self.conn, currency='EUR')

    def review_overlap(self, decision):
        event = next(item for item in self.report()['events'] if item['event_id'] == f'bank:{self.ident}')
        body = {'evidence_version': event['evidence_version'], 'category': 'Groceries',
                'overlap_decision': decision, 'note': 'Checked original bank receipt identifiers'}
        response = self.client.put(f'/api/workspace/native-transactions/{self.ident}/review', json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return body

    def test_duplicate_confirmation_does_not_silently_resurrect_after_ledger_match_disappears(self):
        ledger = self.fixture.txn(-100, currency='EUR')
        original = dict(self.conn.execute('SELECT * FROM bank_transactions WHERE id=?', (self.ident,)).fetchone())
        self.review_overlap('duplicate')
        self.assertEqual(self.report()['totals']['spending'], 100)
        with self.conn:
            self.conn.execute('DELETE FROM transactions WHERE id=?', (ledger,))
        report = self.report()
        self.assertEqual(report['totals']['spending'], 0)
        self.assertTrue(report['partial'])
        self.assertEqual(report['stale_review_count'], 1)
        self.assertEqual(dict(self.conn.execute('SELECT * FROM bank_transactions WHERE id=?', (self.ident,)).fetchone()), original)

    def test_distinct_confirmation_stales_when_another_ledger_candidate_arrives(self):
        self.fixture.txn(-100, currency='EUR')
        body = self.review_overlap('distinct')
        self.assertEqual(self.report()['totals']['spending'], 200)
        self.fixture.txn(-100, currency='EUR')
        report = self.report()
        self.assertEqual(report['totals']['spending'], 200)  # Two ledger rows; changed bank overlap is withheld.
        self.assertTrue(report['partial'])
        self.assertEqual(report['stale_review_count'], 1)
        response = self.client.put(f'/api/workspace/native-transactions/{self.ident}/review', json=body)
        self.assertEqual(response.status_code, 409)

    def test_native_broker_funding_uses_same_context_rule_without_writing_on_read(self):
        with self.conn:
            self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('broker','Trading212','investment','ISA')")
            self.conn.execute("UPDATE bank_transactions SET description='Trading 212',merchant='Trading 212' WHERE id=?", (self.ident,))
        before = self.conn.total_changes
        report = self.report()
        self.assertEqual(report['totals']['spending'], 0)
        self.assertEqual(report['events'][0]['spending_treatment'], 'self_transfer')
        self.assertIn('transfer', report['events'][0]['omission_reason'].lower())
        self.assertEqual(self.conn.total_changes, before)

    def test_foreign_provider_conflict_is_visible_withheld_evidence_not_silent_zero(self):
        with self.conn:
            self.conn.execute("UPDATE bank_transactions SET status='review',review_reason='The provider currency changed',currency='USD' WHERE id=?", (self.ident,))
        before = dict(self.conn.execute('SELECT * FROM bank_transactions WHERE id=?', (self.ident,)).fetchone())
        report = self.report()
        self.assertEqual(report['totals']['spending'], 0)
        self.assertEqual(report['event_count'], 1)
        self.assertTrue(report['partial'])
        self.assertEqual(report['events'][0]['native_amount'], -100)
        self.assertEqual(report['events'][0]['currency'], 'USD')
        self.assertFalse(report['events'][0]['included'])
        self.assertTrue(report['events'][0]['omission_reason'])
        self.assertEqual(dict(self.conn.execute('SELECT * FROM bank_transactions WHERE id=?', (self.ident,)).fetchone()), before)


if __name__ == '__main__':
    unittest.main()

"""Account boundaries, source preservation and real-ledger recurrence failure shapes."""
import json
import sqlite3
import unittest
from datetime import date
from unittest.mock import patch

from app import analytics, recurring_views
from app.db import SCHEMA


class RecurringCalculationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        for account in ('a', 'b'):
            self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES(?,'Bank','current',?)", (account, account))
        self.addCleanup(self.conn.close)

    def payment(self, day, merchant='Service', amount=10, account='a', reviewed=False, kind='merchant'):
        return self.conn.execute(
            'INSERT INTO transactions(account_id,txn_date,merchant,description,amount,category,merchant_kind,classification) '
            'VALUES(?,?,?,?,?,?,?,?)', (account, day, merchant, merchant, -amount, 'Subscriptions', kind,
                                        json.dumps({'reviewed_fields': ['merchant']} if reviewed else {}))).lastrowid

    def patterns(self, today=date(2026, 9, 18), **kwargs):
        return analytics.detect_recurring(self.conn, as_of=today, **kwargs)

    def test_reference_labels_recover_one_stream_and_preserve_original_payments(self):
        ids = [self.payment(f'2026-{month:02d}-20', f'Amazon Prime Ab{month}Cd1234', 8.99) for month in (6, 7, 8)]
        before = list(self.conn.execute('SELECT * FROM transactions'))
        patterns = self.patterns()
        self.assertEqual(len(patterns), 1)
        row = patterns[0]
        self.assertEqual((row['merchant'], row['monthly_cost'], row['next_expected']), ('Amazon Prime', 8.99, '2026-09-20'))
        self.assertTrue(row['forecast_eligible'])
        details = recurring_views.overview(self.conn, patterns, as_of=date(2026, 9, 18))
        self.assertEqual(details['candidates'], [])
        self.assertEqual({p['id'] for p in details['patterns'][0]['payments']}, set(ids))
        self.assertEqual(len({p['description'] for p in details['patterns'][0]['payments']}), 3)
        self.assertEqual(before, list(self.conn.execute('SELECT * FROM transactions')))

    def test_punctuation_and_case_share_evidence_but_accounts_do_not(self):
        for account in ('a', 'b'):
            for month, name in zip((6, 7, 8), ('Netflix Com', 'Netflix.com', 'NETFLIX COM')):
                self.payment(f'2026-{month:02d}-15', name, account=account)
        rows = self.patterns()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['n_occurrences'] == 3 for row in rows))
        self.assertEqual({row['account_id'] for row in rows}, {'a', 'b'})

    def test_direct_detection_reads_the_ledger_once(self):
        for month in (6, 7, 8):
            self.payment(f'2026-{month:02d}-20')
        with patch.object(analytics, '_rows', wraps=analytics._rows) as read:
            self.assertEqual(len(self.patterns()), 1)
        self.assertEqual(read.call_count, 1)

    def test_user_reviewed_names_people_and_unrecognized_suffixes_are_not_folded(self):
        for month in (6, 7, 8):
            self.payment(f'2026-{month:02d}-20', f'Amazon Prime Ab{month}Cd1234', reviewed=True)
            self.payment(f'2026-{month:02d}-21', f'Other Service Ab{month}Cd1234')
            self.payment(f'2026-{month:02d}-22', f'Person {month}', kind='person')
        self.assertEqual(self.patterns(), [])

    def test_same_reviewed_spelling_keeps_its_unreviewed_payments(self):
        for month in (6, 7, 8):
            self.payment(f'2026-{month:02d}-20', reviewed=month == 8)
        self.assertEqual(self.patterns()[0]['n_occurrences'], 3)

    def test_sustained_price_change_stays_forecastable_but_single_spike_does_not_raise_estimate(self):
        for month, amount in zip(range(4, 10), (23, 23, 23, 23, 27.97, 27.97)):
            self.payment(f'2026-{month:02d}-01', 'Utilities', amount)
        for month, amount in zip(range(5, 10), (49, 49, 49, 59, 49)):
            self.payment(f'2026-{month:02d}-01', 'Internet', amount)
        rows = {r['merchant']: r for r in self.patterns()}
        self.assertEqual(rows['Utilities']['status'], 'price_change')
        self.assertEqual(rows['Utilities']['current_amount'], 27.97)
        self.assertTrue(rows['Utilities']['forecast_eligible'])
        self.assertEqual(rows['Internet']['current_amount'], 49)
        self.assertEqual(rows['Internet']['monthly_cost'], 49)
        self.assertEqual(rows['Internet']['status'], 'active')
        loose = {r['merchant']: r for r in self.patterns(amount_tol=.3)}
        self.assertEqual(loose['Utilities']['status'], 'active')

    def test_future_subscription_is_neither_pattern_nor_candidate_evidence(self):
        ident = self.payment('2027-01-01', 'Future service')
        detail = recurring_views.overview(self.conn, self.patterns(), as_of=date(2026, 9, 18))
        self.assertEqual(detail, {'patterns': [], 'candidates': []})
        self.assertIsNotNone(self.conn.execute('SELECT id FROM transactions WHERE id=?', (ident,)).fetchone())

    def test_unrepresentable_next_date_does_not_break_detection(self):
        for month in (10, 11, 12):
            self.payment(f'9999-{month:02d}-28')
        self.assertEqual(self.patterns(today=date(9999, 12, 31)), [])

    def test_off_schedule_extras_do_not_set_the_regular_amount(self):
        ids = [self.payment(f'2026-{month:02d}-05') for month in range(1, 6)]
        ids += [self.payment(day, amount=100) for day in ('2026-04-15', '2026-04-16')]
        row = self.patterns(today=date(2026, 5, 6))[0]
        self.assertTrue(row['forecast_eligible'])
        self.assertEqual(row['monthly_cost'], 10)
        self.assertEqual(row['schedule_evidence_count'], 5)
        self.assertEqual(row['n_occurrences'], 7)
        self.assertEqual(set(row['txn_ids']), set(ids))
        self.assertIn('5 payment dates matching this schedule', row['amount_basis'])

    def test_sparse_variable_billing_does_not_become_a_fixed_commitment_by_dropping_an_extra(self):
        for day, amount in [('2026-06-30', 89), ('2026-07-21', 172.28),
                            ('2026-07-25', 91.01), ('2026-08-21', 200)]:
            self.payment(day, 'Cloud software', amount)
        row = self.patterns()[0]
        self.assertEqual(row['schedule_evidence_count'], 3)
        self.assertEqual(row['pattern_type'], 'variable_cost')
        self.assertFalse(row['forecast_eligible'])

    def test_old_exports_do_not_become_confirmed_cancellations(self):
        for month in (1, 2, 3):
            self.payment(f'2026-{month:02d}-05')
        row = self.patterns()[0]
        self.assertTrue(row['data_stale'])
        self.assertNotEqual(row['status'], 'ended')
        self.assertFalse(row['forecast_eligible'])
        self.payment('2026-09-01', 'Unrelated')
        row = self.patterns()[0]
        self.assertEqual(row['status'], 'ended')
        self.assertEqual(row['monthly_cost'], 0)
        self.assertEqual(row['historical_monthly_cost'], 10)


if __name__ == '__main__':
    unittest.main()

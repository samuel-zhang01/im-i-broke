"""Credit evidence checks must never manufacture a paid-off or live balance."""
from datetime import date
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.db import init_db
from app import lunchflow_store, wealth_checks


TODAY = date(2026, 9, 18)


class CreditBalanceEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.conn = init_db(Path(self.temp.name) / 'checks.db')
        self.addCleanup(self.conn.close)
        lunchflow_store.ensure_schema(self.conn)
        self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('credit','HSBC','credit','Card')")
        self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('cash','HSBC','debit','Cash')")
        self.conn.execute("INSERT INTO statements(id,account_id,source_file,period_end,closing_balance,reconciled) VALUES(1,'credit','synthetic','2026-08-16',955.73,1)")
        self.account = {'id': 'statement:credit', 'kind': 'credit', 'institution': 'HSBC',
                        'source': 'lunchflow', 'native_value': -7145.65, 'currency': 'GBP',
                        'as_of': '2026-09-18', 'drilldown': {'account_id': 'credit'}}

    def transaction(self, amount, when, account='credit', description='Synthetic', meta=None, **flags):
        self.conn.execute('''INSERT INTO transactions(account_id,txn_date,amount,description,meta,
            exclude_from_analysis,spending_treatment) VALUES(?,?,?,?,?,?,?)''',
            (account, when, amount, description, json.dumps(meta or {}),
             int(flags.get('excluded', False)), flags.get('treatment', 'normal')))

    def check(self):
        wealth_checks.annotate_credit_balance_checks(self.conn, [self.account], today=TODAY)
        return self.account['balance_check']

    def bank(self, amount, when, *, status='review', pending=False):
        self.conn.execute("""INSERT OR IGNORE INTO bank_connections(id,account_id,name,institution_name,
            currency,status,raw,first_seen_at,last_seen_at) VALUES('feed','credit','Card','HSBC','GBP','ACTIVE','{}','2026-09-18','2026-09-18')""")
        self.conn.execute('''INSERT INTO bank_transactions(connection_id,provider_id,txn_date,amount,
            currency,is_pending,status,raw,first_seen_at,last_seen_at) VALUES('feed',?,?,?,?,?,?,'{}','2026-09-18','2026-09-18')''',
            (str(self.conn.total_changes), when, amount, 'GBP', int(pending), status))

    def test_material_difference_does_not_replace_reported_balance(self):
        self.transaction(1000, '2026-08-17', excluded=True, treatment='transfer')
        self.transaction(-1489.98, '2026-09-17')
        check = self.check()
        self.assertEqual(check['transaction_estimate'], -1445.71)
        self.assertEqual(check['difference'], -5699.94)
        self.assertEqual(check['status'], 'discrepancy')
        self.assertTrue(check['requires_review'])
        self.assertEqual(self.account['native_value'], -7145.65)
        self.assertIsNone(check['bank_as_of'])

    def test_uses_posting_dates_and_excludes_future_activity(self):
        self.transaction(-20, '2026-08-15', meta={'posting_date': '2026-08-17'})
        self.transaction(10, '2026-08-16', meta={'posted_date': '2026-08-18'})
        self.transaction(-50, '2026-09-18', meta={'posting_date': '2026-09-19'})
        self.transaction(-500, '2026-09-20')
        self.assertEqual(self.check()['transaction_estimate'], -965.73)

    def test_reviews_and_pending_are_caveats_not_additional_ledger_changes(self):
        self.bank(-13.85, '2026-08-17')
        self.bank(-32.14, '2026-09-17', status='pending', pending=True)
        self.bank(-999, '2026-08-17', status='expired', pending=True)
        self.bank(-100, '2026-08-15')
        check = self.check()
        self.assertEqual(check['review_count'], 1)
        self.assertEqual(check['pending_count'], 1)
        self.assertEqual(check['pending_amount'], 32.14)
        self.assertEqual(check['transaction_estimate'], -955.73)

    def test_pending_can_explain_small_difference_without_asserting_verified_debt(self):
        self.account['native_value'] = -965.73
        self.bank(-10, '2026-09-17', status='pending', pending=True)
        check = self.check()
        self.assertEqual(check['status'], 'no_discrepancy')
        self.assertFalse(check['requires_review'])
        self.assertIn('does not prove', check['message'])

    def test_paid_statement_stays_unknown_current_and_exposes_safe_payment_evidence(self):
        self.account.update(source='statement', institution='AMEX', native_value=-1167.6, as_of='2026-09-03')
        self.conn.execute("UPDATE accounts SET bank='AMEX' WHERE id='credit'")
        self.conn.execute("UPDATE statements SET period_end='2026-09-03',closing_balance=1167.6")
        self.transaction(-1167.6, '2026-09-04', 'cash', 'AMERICAN EXP 0000 private reference')
        self.transaction(-813.23, '2026-09-11', 'cash', 'AMERICAN EXPRESS private reference')
        check = self.check()
        self.assertEqual(check['status'], 'statement_only')
        self.assertTrue(check['requires_review'])
        self.assertEqual(check['potential_repayments'], [
            {'date': '2026-09-04', 'amount': 1167.6, 'matches_statement_balance': True},
            {'date': '2026-09-11', 'amount': 813.23, 'matches_statement_balance': False}])
        self.assertEqual(check['transaction_estimate'], -1167.6)
        self.assertNotIn('private reference', json.dumps(check))
        self.assertEqual(self.account['native_value'], -1167.6)

    def test_unreconciled_conflicting_and_foreign_evidence_never_yields_estimate(self):
        self.conn.execute('UPDATE statements SET reconciled=0')
        self.assertIsNone(self.check()['transaction_estimate'])
        self.conn.execute('UPDATE statements SET reconciled=1')
        self.conn.execute("INSERT INTO statements(account_id,source_file,period_end,closing_balance,reconciled) VALUES('credit','conflict','2026-08-16',200,1)")
        self.assertIsNone(self.check()['transaction_estimate'])
        self.conn.execute("DELETE FROM statements WHERE source_file='conflict'")
        self.account['currency'] = 'USD'
        self.assertIsNone(self.check()['transaction_estimate'])

    def test_credit_surplus_sign_is_preserved(self):
        self.conn.execute('UPDATE statements SET closing_balance=-20')
        self.transaction(30, '2026-09-01')
        self.account['native_value'] = 50
        check = self.check()
        self.assertEqual(check['statement_balance'], 20)
        self.assertEqual(check['transaction_estimate'], 50)
        self.assertEqual(check['status'], 'no_discrepancy')

    def test_minimal_database_and_non_credit_accounts_remain_usable(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.execute('CREATE TABLE statements(id,account_id,period_end,closing_balance,reconciled)')
        conn.execute("INSERT INTO statements VALUES(1,'credit','2026-09-03',100,1)")
        cash = {'id': 'statement:cash', 'kind': 'cash'}
        wealth_checks.annotate_credit_balance_checks(conn, [self.account, cash], today=TODAY)
        self.assertIsNone(self.account['balance_check']['transaction_estimate'])
        self.assertNotIn('balance_check', cash)


if __name__ == '__main__':
    unittest.main()

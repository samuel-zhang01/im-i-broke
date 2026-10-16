"""Current credit projections preserve evidence and never manufacture payoff."""
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

from app.db import init_db
from app import balance_resolution, bank_balance_rules, lunchflow_store


TODAY = date(2026, 9, 18)
STAMP = '2026-09-18T11:59:28+00:00'


class CreditBalanceResolutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.conn = init_db(Path(temporary.name) / 'resolution.db')
        self.addCleanup(self.conn.close)
        lunchflow_store.ensure_schema(self.conn)
        self.conn.execute("INSERT INTO accounts(id,bank,account_type) VALUES('card','HSBC','credit')")
        self.conn.execute("INSERT INTO accounts(id,bank,account_type) VALUES('cash','HSBC','debit')")
        self.conn.execute("""INSERT INTO statements(id,account_id,source_file,period_end,closing_balance,reconciled)
            VALUES(1,'card','synthetic','2026-08-16',955.73,1)""")
        self.conn.execute("""INSERT INTO bank_connections(id,account_id,name,institution_name,currency,
            status,raw,first_seen_at,last_seen_at,last_synced_at,balance_multiplier)
            VALUES('feed','card','Card','HSBC','GBP','ACTIVE','{}',?,?,?,-1)""", (STAMP, STAMP, STAMP))
        self.bank(-1, '2026-08-15')
        self.bank(1000, '2026-08-17')
        self.bank(-1503.83, '2026-09-17')
        self.bank(-32.14, '2026-09-17', status='pending', pending=True)
        self.args = dict(reported_balance=-7145.65, source='lunchflow',
                         currency='GBP', reported_as_of=STAMP, today=TODAY)

    def bank(self, amount, when, *, status='imported', pending=False, linked=True, meta=None):
        transaction_id = None
        if not pending and linked:
            transaction_id = self.conn.execute("""INSERT INTO transactions(account_id,txn_date,
                amount,meta) VALUES('card',?,?,?)""", (when, amount, json.dumps(meta or {}))).lastrowid
        return self.conn.execute("""INSERT INTO bank_transactions(connection_id,provider_id,txn_date,
            amount,currency,is_pending,status,transaction_id,raw,first_seen_at,last_seen_at)
            VALUES('feed',?,?,?,'GBP',?,?,?,'{}',?,?)""",
            (str(self.conn.total_changes), when, amount, int(pending), status, transaction_id, STAMP, STAMP)).lastrowid

    def resolve(self, **overrides):
        return balance_resolution.resolve_credit_balance(self.conn, 'card', **(self.args | overrides))

    def assert_unknown(self, resolution):
        self.assertEqual(resolution['basis'], 'unknown')
        self.assertTrue(resolution['current_unknown'])
        self.assertIsNone(resolution['effective_value'])
        self.assertEqual(resolution['reported_balance'], -7145.65)

    def test_conflicting_balance_uses_estimate_and_preserves_source_without_writes(self):
        before = self.conn.total_changes
        result = self.resolve()
        self.assertEqual(result['effective_value'], -1459.56)
        self.assertEqual(result['effective_as_of'], '2026-09-18')
        self.assertEqual(result['reported_balance'], -7145.65)
        self.assertEqual(result['reported_as_of'], STAMP)
        self.assertEqual(result['basis'], 'statement_activity_estimate')
        self.assertTrue(result['estimated'])
        self.assertFalse(result['current_unknown'])
        self.assertEqual(result['pending_amount'], 32.14)
        self.assertEqual(result['coverage']['booked_count'], 2)
        self.assertTrue(result['balance_check']['requires_review'])
        self.assertIn('not a confirmed live balance', result['explanation'])
        self.assertEqual(self.conn.total_changes, before)

    def test_unresolved_post_statement_rows_make_disputed_balance_unknown(self):
        row_id = self.bank(-13.85, '2026-09-17', status='review', linked=False)
        self.assert_unknown(self.resolve())
        self.conn.execute("UPDATE bank_transactions SET status='waiting_mapping' WHERE id=?", (row_id,))
        self.assert_unknown(self.resolve())

    def test_old_overlap_reviews_do_not_block_current_estimate(self):
        self.bank(-100, '2026-08-15', status='review', linked=False)
        self.assertEqual(self.resolve()['effective_value'], -1459.56)

    def test_stale_failed_absent_or_inactive_sync_cannot_support_estimate(self):
        for assignment in ["last_synced_at='2026-09-14'", "last_synced_at=NULL",
                           "last_error='Provider failed'", "present=0", "status='INACTIVE'"]:
            with self.subTest(assignment=assignment):
                self.conn.execute('SAVEPOINT fresh_connection')
                self.conn.execute('UPDATE bank_connections SET ' + assignment)
                self.assert_unknown(self.resolve())
                self.conn.execute('ROLLBACK TO fresh_connection')
                self.conn.execute('RELEASE fresh_connection')

    def test_cache_must_reach_statement_boundary(self):
        self.conn.execute("DELETE FROM bank_transactions WHERE txn_date<='2026-08-16'")
        self.assert_unknown(self.resolve())

    def test_estimate_requires_a_recent_statement(self):
        self.conn.execute("UPDATE statements SET period_end='2026-06-01'")
        self.bank(-1, '2026-05-31')
        self.assert_unknown(self.resolve())

    def test_broken_link_or_changed_amount_cannot_support_estimate(self):
        self.conn.execute("UPDATE bank_transactions SET amount=-2000 WHERE txn_date='2026-09-17' AND is_pending=0")
        self.assert_unknown(self.resolve())

    def test_posting_dates_determine_estimate_and_cannot_extend_past_sync(self):
        self.bank(-20, '2026-08-15', meta={'posting_date': '2026-08-17'})
        self.bank(-50, '2026-09-18', meta={'posting_date': '2026-09-19'})
        self.assertEqual(self.resolve()['effective_value'], -1479.56)
        self.conn.execute("UPDATE bank_connections SET last_synced_at='2026-09-17T10:00:00+00:00'")
        self.bank(-10, '2026-09-16', meta={'posting_date': '2026-09-18'})
        self.assert_unknown(self.resolve())

    def test_matching_provider_balance_keeps_reported_value(self):
        result = self.resolve(reported_balance=-1459.56)
        self.assertEqual(result['effective_value'], -1459.56)
        self.assertEqual(result['basis'], 'reported')
        self.assertFalse(result['estimated'])
        self.assertEqual(result['effective_as_of'], '2026-09-18')
        self.assertEqual(result['reported_as_of'], STAMP)

    def test_statement_repayment_makes_current_unknown_without_zeroing(self):
        self.conn.execute("UPDATE accounts SET bank='AMEX' WHERE id='card'")
        self.conn.execute("UPDATE statements SET period_end='2026-09-03',closing_balance=1167.60")
        self.conn.execute("""INSERT INTO transactions(account_id,txn_date,amount,description)
            VALUES('cash','2026-09-04',-1167.60,'AMERICAN EXP payment')""")
        result = self.resolve(source='statement', reported_balance=-1167.6, reported_as_of='2026-09-03')
        self.assertIsNone(result['effective_value'])
        self.assertEqual(result['basis'], 'unknown')
        self.assertEqual(result['reported_balance'], -1167.6)
        self.assertEqual(result['balance_check']['statement_balance'], -1167.6)
        self.assertTrue(result['balance_check']['potential_repayments'][0]['matches_statement_balance'])
        self.assertFalse(result['estimated'])

    def test_statement_without_payment_remains_latest_known_value(self):
        result = self.resolve(source='statement', reported_balance=-955.73, reported_as_of='2026-08-16')
        self.assertEqual(result['effective_value'], -955.73)
        self.assertEqual(result['basis'], 'reported')

    def test_overpaid_statement_projection_preserves_positive_asset(self):
        self.conn.execute('UPDATE statements SET closing_balance=-20')
        self.conn.execute("DELETE FROM bank_transactions WHERE txn_date>'2026-08-16'")
        self.conn.execute("DELETE FROM transactions WHERE txn_date>'2026-08-16'")
        self.bank(30, '2026-09-17')
        result = self.resolve(reported_balance=-7000)
        self.assertEqual(result['effective_value'], 50)
        self.assertTrue(result['estimated'])

    def test_insufficient_statement_evidence_never_manufactures_estimate(self):
        self.conn.execute('UPDATE statements SET reconciled=0')
        result = self.resolve()
        self.assertEqual(result['effective_value'], -7145.65)
        self.assertFalse(result['estimated'])
        self.assertIsNone(result['balance_check']['transaction_estimate'])

    def test_unsupported_balance_is_unknown(self):
        result = self.resolve(reported_balance=None)
        self.assertIsNone(result['effective_value'])
        self.assertEqual(result['basis'], 'unknown')
        self.assertTrue(result['current_unknown'])

    def test_confirmed_available_credit_uses_limit_not_statement_estimate_or_pending(self):
        bank_balance_rules.set_rule(self.conn, 'feed', balance_type='available_credit', credit_limit=8750, currency='GBP')
        before = self.conn.total_changes
        result = self.resolve()
        self.assertEqual(result['effective_value'], -1604.35)
        self.assertEqual(result['available_credit'], 7145.65)
        self.assertEqual(result['credit_limit'], 8750)
        self.assertEqual(result['basis'], 'available_credit')
        self.assertFalse(result['estimated'])
        self.assertEqual(result['reported_balance'], -7145.65)
        self.assertEqual(result['pending_amount'], 32.14)
        self.assertEqual(result['balance_check']['transaction_estimate'], -1459.56)
        self.assertEqual(result['balance_check']['status'], 'available_credit')
        self.assertFalse(result['balance_check']['requires_review'])
        self.assertEqual(self.conn.total_changes, before)

    def test_confirmed_source_type_does_not_require_statement_or_linked_transactions(self):
        bank_balance_rules.set_rule(self.conn, 'feed', balance_type='available_credit', credit_limit=8750, currency='GBP')
        self.bank(-99, '2026-09-17', status='review', linked=False)
        self.assertEqual(self.resolve()['effective_value'], -1604.35)
        self.conn.execute('DELETE FROM statements')
        self.assertEqual(self.resolve()['effective_value'], -1604.35)
        self.assertEqual(self.resolve(reported_balance=-7000)['effective_value'], -1750)

    def test_available_credit_recovers_mapping_sign_and_preserves_over_limit_or_surplus(self):
        bank_balance_rules.set_rule(self.conn, 'feed', balance_type='available_credit', credit_limit=8750, currency='GBP')
        for multiplier in (-1, 1):
            self.conn.execute('UPDATE bank_connections SET balance_multiplier=?', (multiplier,))
            for available, expected in ((7145.65, -1604.35), (-100, -8850), (8800, 50), (8750, 0), (0, -8750)):
                with self.subTest(multiplier=multiplier, available=available):
                    result = self.resolve(reported_balance=available * multiplier)
                    self.assertEqual(result['available_credit'], available)
                    self.assertEqual(result['effective_value'], expected)
                    self.assertFalse(result['estimated'])

    def test_available_credit_rule_does_not_apply_to_statement_or_missing_balance(self):
        bank_balance_rules.set_rule(self.conn, 'feed', balance_type='available_credit', credit_limit=8750, currency='GBP')
        result = self.resolve(source='statement', reported_balance=-955.73, reported_as_of='2026-08-16')
        self.assertEqual(result['effective_value'], -955.73)
        self.assertEqual(result['basis'], 'reported')
        self.assertIsNone(self.resolve(reported_balance=None)['effective_value'])

    def test_disabling_available_credit_rule_restores_evidence_based_fallback(self):
        bank_balance_rules.set_rule(self.conn, 'feed', balance_type='available_credit', credit_limit=8750, currency='GBP')
        bank_balance_rules.set_rule(self.conn, 'feed', balance_type='reported')
        result = self.resolve()
        self.assertEqual(result['effective_value'], -1459.56)
        self.assertEqual(result['basis'], 'statement_activity_estimate')


if __name__ == '__main__':
    unittest.main()

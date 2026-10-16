"""Currency and preservation checks for bank balances in the existing app."""
import tempfile
import unittest
from pathlib import Path
from datetime import date, datetime, timezone
from unittest.mock import patch

from app.db import init_db
from app import bank_balance_rules, lunchflow_store as banks, planning, wealth
from app import lunchflow_client
from app.ingest import _store_statement
from app.parsers.base import ParsedStatement, RawTxn


class BankingWealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.conn = init_db(Path(self.temp.name) / 'test.db')
        self.addCleanup(self.conn.close)
        banks.ensure_schema(self.conn)
        for fn, value in [('load_portfolio', None), ('list_account_observations', []), ('list_account_estimates', [])]:
            mocked = patch.object(wealth.snaptrade_store, fn, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def account(self, name, code='GBP', amount=100, multiplier=1, kind='current'):
        self.conn.execute('INSERT INTO accounts(id,bank,account_type,display_name,currency) VALUES(?,?,?,?,?)',
                          (name, 'Synthetic', kind, name, code))
        self.conn.execute('''INSERT INTO bank_connections(id,account_id,name,institution_name,currency,status,
            balance_multiplier,raw,first_seen_at,last_seen_at) VALUES(?,?,?,'Synthetic',?,'ACTIVE',?,'{}',?,?)''',
                          (name, name, name, code, multiplier, '2026-09-18T08:00:00+00:00', '2026-09-18T08:00:00+00:00'))
        self.conn.execute('INSERT INTO bank_balance_observations(connection_id,amount,currency,fetched_at) VALUES(?,?,?,?)',
                          (name, amount, code, '2026-09-18T08:00:00+00:00'))

    def test_feed_extends_statement_account_without_double_counting(self):
        self.account('cash', amount=150)
        self.conn.execute("INSERT INTO statements(account_id,source_file,period_end,closing_balance,reconciled) VALUES('cash','synthetic','2026-08-31',100,1)")
        result = wealth.bundle(self.conn, today=date(2026, 9, 18))
        self.assertEqual(len(result['accounts']), 1)
        self.assertEqual(result['summary']['net_worth'], 150)
        self.assertEqual(result['accounts'][0]['source'], 'lunchflow')
        self.assertEqual(result['points'][0]['net_worth'], 100)
        self.assertEqual(self.conn.execute('SELECT closing_balance FROM statements').fetchone()[0], 100)

    def test_latest_same_day_snapshot_wins_and_credit_is_signed_once(self):
        self.account('credit', amount=200, multiplier=-1, kind='credit')
        self.conn.execute("INSERT INTO bank_balance_observations(connection_id,amount,currency,fetched_at) VALUES('credit',180,'GBP','2026-09-18T12:00:00+00:00')")
        result = wealth.bundle(self.conn, today=date(2026, 9, 18))
        self.assertEqual(result['summary']['net_worth'], -180)
        self.assertEqual(result['summary']['liabilities'], 180)
        self.assertEqual(planning.account_snapshot(self.conn, date(2026, 9, 18))[0]['balance'], -180)

    def test_foreign_balance_never_treated_as_gbp_without_fx(self):
        self.account('usd', code='USD', amount=100)
        snap = planning.account_snapshot(self.conn, date(2026, 9, 18))[0]
        self.assertIsNone(snap['balance'])
        self.assertEqual(snap['native_balance'], 100)
        self.assertEqual(snap['currency'], 'USD')
        self.assertIsNone(wealth.bundle(self.conn, today=date(2026, 9, 18))['summary']['net_worth'])
        self.conn.execute("INSERT INTO reference_fx VALUES('2026-09-18','USD',0.75,'Synthetic','2026-09-18','2026-09-18',NULL)")
        self.assertEqual(planning.account_snapshot(self.conn, date(2026, 9, 18))[0]['balance'], 75)
        self.assertEqual(wealth.bundle(self.conn, today=date(2026, 9, 18))['summary']['net_worth'], 75)

    def test_inactive_connection_keeps_cache_and_marks_stale(self):
        self.account('cash', amount=123)
        self.conn.execute("UPDATE bank_connections SET status='DISCONNECTED'")
        result = wealth.bundle(self.conn, today=date(2026, 9, 18))
        self.assertEqual(result['summary']['net_worth'], 123)
        self.assertTrue(result['accounts'][0]['stale'])

    def test_bank_net_balance_and_wealth_debt_have_explicit_shared_provenance(self):
        self.account('cash', amount=500)
        self.account('credit', amount=700, multiplier=-1, kind='credit')
        self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('manual','Synthetic','credit','Manual card')")
        self.conn.execute("INSERT INTO statements(account_id,source_file,period_end,closing_balance,reconciled) VALUES('credit','credit.pdf','2026-09-01',100,1)")
        self.conn.execute("INSERT INTO statements(account_id,source_file,period_end,closing_balance,reconciled) VALUES('manual','manual.pdf','2026-09-01',50,1)")
        self.conn.execute("INSERT INTO transactions(account_id,txn_date,amount) VALUES('credit','2026-09-02',-20)")
        self.conn.execute("INSERT INTO transactions(account_id,txn_date,amount) VALUES('credit','2026-08-30',-10)")
        self.conn.execute("UPDATE bank_connections SET last_synced_at='2026-09-18T08:00:00+00:00' WHERE id='credit'")
        for row in self.conn.execute("SELECT id,txn_date,amount FROM transactions WHERE account_id='credit'").fetchall():
            self.conn.execute('''INSERT INTO bank_transactions(connection_id,provider_id,txn_date,amount,currency,status,
                transaction_id,raw,first_seen_at,last_seen_at) VALUES('credit',?,?,?,'GBP','imported',?,'{}',?,?)''',
                (str(row['id']),row['txn_date'],row['amount'],row['id'],'2026-09-18T08:00:00+00:00','2026-09-18T08:00:00+00:00'))
        bank = banks.summary(self.conn, today=date(2026, 9, 18))
        result = wealth.bundle(self.conn, today=date(2026, 9, 18))
        self.assertEqual(bank['totals_by_currency']['GBP'], 380)
        self.assertEqual(bank['balance_totals_by_currency']['GBP'], {'assets': 500, 'liabilities': 120, 'net': 380})
        self.assertEqual(bank['statement_liabilities_by_currency'], {'GBP': 50})
        self.assertEqual(result['summary']['liabilities'], 170)
        self.assertEqual(result['summary']['net_worth'], 330)
        self.assertEqual(result['summary']['estimated_accounts'], 1)
        self.assertEqual(result['summary']['balance_check_accounts'], 2)
        self.assertTrue(result['summary']['partial'])
        bank_check = next(check for check in bank['balance_checks'] if check['account_id'] == 'credit')
        wealth_check = next(account['balance_check'] for account in result['accounts'] if account['id'] == 'statement:credit')
        for field in ('status', 'reported_balance', 'transaction_estimate', 'difference'):
            self.assertEqual(bank_check[field], wealth_check[field])
        self.assertEqual(bank_check['status'], 'discrepancy')
        self.assertEqual(bank_check['transaction_estimate'], -120)
        self.assertEqual(bank_check['reported_balance'], -700)
        self.assertEqual(self.conn.execute("SELECT amount FROM bank_balance_observations WHERE connection_id='credit'").fetchone()[0], 700)
        credit = next(a for a in result['accounts'] if a['id'] == 'statement:credit')
        self.assertEqual(credit['reported_value'], -700)
        self.assertEqual(credit['value'], -120)
        self.assertTrue(credit['estimated'])
        self.assertEqual(result['points'][-1]['net_worth'], 330)
        self.assertEqual(next(a for a in planning.account_snapshot(self.conn, date(2026, 9, 18)) if a['id']=='credit')['balance'], -120)

    def test_paid_statement_with_no_new_card_feed_is_unknown_in_both_views(self):
        self.account('cash', amount=500)
        self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('amex','AMEX','credit','Amex card')")
        self.conn.execute("INSERT INTO statements(account_id,source_file,period_end,closing_balance,reconciled) VALUES('amex','amex.pdf','2026-09-01',50,1)")
        self.conn.execute("INSERT INTO transactions(account_id,txn_date,amount,description) VALUES('cash','2026-09-02',-50,'AMEX PAYMENT')")
        bank = banks.summary(self.conn, today=date(2026, 9, 18))
        result = wealth.bundle(self.conn, today=date(2026, 9, 18))
        card = next(a for a in result['accounts'] if a['id']=='statement:amex')
        self.assertIsNone(card['value'])
        self.assertEqual(card['reported_value'], -50)
        self.assertEqual(card['valuation']['basis'], 'unknown')
        self.assertEqual(result['summary']['net_worth'], 500)
        self.assertEqual(result['summary']['unknown_liability_accounts'], 1)
        self.assertTrue(result['summary']['partial'])
        self.assertEqual(bank['statement_liabilities_by_currency'], {})
        self.assertEqual(bank['statement_reported_liabilities_by_currency'], {'GBP':50})
        self.assertEqual(bank['unknown_balance_accounts'], 1)
        local = next(a for a in bank['local_accounts'] if a['id']=='amex')
        self.assertIsNone(local['balance'])
        self.assertEqual(local['reported_balance'], -50)
        # Historical statement evidence remains a liability on its own date.
        self.assertEqual(result['points'][0]['liabilities'], 50)

    @patch('app.lunchflow_store.datetime', wraps=datetime)
    def test_available_credit_rule_is_shared_and_tracks_new_provider_observations(self, clock):
        # Freshness uses an instant, independently of the requested history date.
        clock.now.return_value = datetime(2026, 9, 18, 13, tzinfo=timezone.utc)
        today = date(2026, 9, 18)
        self.account('cash', amount=2447.60)
        self.account('credit', amount=7145.65, multiplier=-1, kind='credit')
        bank_balance_rules.set_rule(self.conn, 'credit', balance_type='available_credit', credit_limit=8750, currency='GBP')
        for expected in (-1604.35, -750):
            bank = banks.summary(self.conn, today=today)
            result = wealth.bundle(self.conn, today=today)
            snapshot = next(a for a in planning.account_snapshot(self.conn, today) if a['id']=='credit')
            card = next(a for a in result['accounts'] if a['id']=='statement:credit')
            provider = next(a for a in bank['accounts'] if a['id']=='credit')
            self.assertEqual(provider['balance']['amount'], expected)
            self.assertEqual(card['native_value'], expected)
            self.assertEqual(snapshot['native_balance'], expected)
            self.assertEqual(provider['balance_rule']['credit_limit'], 8750)
            self.assertEqual(result['summary']['liabilities'], -expected)
            self.assertEqual(result['summary']['net_worth'], round(2447.60 + expected, 2))
            self.assertEqual(bank['totals_by_currency']['GBP'], result['summary']['net_worth'])
            self.assertEqual(result['points'][-1]['net_worth'], result['summary']['net_worth'])
            self.assertEqual(result['summary']['estimated_accounts'], 0)
            self.assertEqual(bank['estimated_accounts'], 0)
            self.assertEqual(result['summary']['balance_check_accounts'], 0)
            self.assertFalse(result['summary']['partial'])
            self.assertEqual(provider['valuation']['basis'], 'available_credit')
            if expected == -1604.35:
                with self.conn:
                    banks.record_account_result(self.conn, 'credit', {'balance':{'amount':8000,'currency':'GBP'},
                        'transactions':[], 'errors':[]}, '2026-09-18T12:00:00+00:00')
        self.assertEqual([r[0] for r in self.conn.execute("SELECT amount FROM bank_balance_observations WHERE connection_id='credit' ORDER BY id")], [7145.65,8000])
        clock.now.return_value = datetime(2026, 9, 21, 13, tzinfo=timezone.utc)
        aged = wealth.bundle(self.conn, today=date(2026, 9, 21))
        self.assertTrue(aged['summary']['partial'])
        self.assertEqual(aged['summary']['net_worth'], result['summary']['net_worth'])


    def test_newer_statement_supersedes_old_feed_in_all_current_totals(self):
        today = date(2026, 9, 18)
        self.account('cash', amount=500)
        self.account('amex', amount=700, multiplier=-1, kind='credit')
        self.conn.execute("UPDATE accounts SET bank='AMEX' WHERE id='amex'")
        self.conn.execute("UPDATE bank_balance_observations SET fetched_at='2026-09-14T08:00:00+00:00' WHERE connection_id='amex'")
        self.conn.execute("INSERT INTO statements(account_id,source_file,period_end,closing_balance,reconciled) VALUES('amex','amex.pdf','2026-09-15',50,1)")
        self.conn.execute("INSERT INTO transactions(account_id,txn_date,amount,description) VALUES('cash','2026-09-16',-50,'AMEX PAYMENT')")
        raw = banks.latest_balances(self.conn, resolve=False)['amex']
        before = self.conn.total_changes
        bank = banks.summary(self.conn, today=today)
        result = wealth.bundle(self.conn, today=today)
        snapshot = next(a for a in planning.account_snapshot(self.conn, today) if a['id'] == 'amex')
        card = next(a for a in result['accounts'] if a['id'] == 'statement:amex')
        provider = next(a for a in bank['accounts'] if a['id'] == 'amex')
        local = next(a for a in bank['local_accounts'] if a['id'] == 'amex')
        for account in (card, snapshot, provider, local):
            self.assertEqual(account['valuation']['basis'], 'unknown')
            self.assertEqual(account['valuation']['reported_source'], 'statement')
            self.assertEqual(account['valuation']['reported_balance'], -50)
            self.assertEqual(account['valuation']['reported_as_of'], '2026-09-15')
        self.assertIsNone(card['value'])
        self.assertIsNone(snapshot['balance'])
        self.assertIsNone(provider['balance']['amount'])
        self.assertIsNone(local['balance'])
        self.assertEqual(provider['balance']['reported_amount'], -50)
        self.assertEqual(provider['balance']['provider_reported_amount'], -700)
        self.assertEqual(bank['totals_by_currency'], {'GBP': 500})
        self.assertEqual(result['summary']['net_worth'], 500)
        self.assertEqual(result['summary']['unknown_liability_accounts'], 1)
        self.assertTrue(result['summary']['partial'])
        self.assertEqual(card['reported_value'], -50)
        self.assertEqual(next(p for p in result['points'] if p['date'] == '2026-09-15')['liabilities'], 50)
        self.assertEqual(banks.latest_balances(self.conn, resolve=False)['amex'], raw)
        self.assertEqual(self.conn.total_changes, before)

        # Without later repayment evidence, the newer statement is retained
        # as a dated known balance in every consumer, not the older feed.
        self.conn.execute("DELETE FROM transactions WHERE account_id='cash'")
        bank = banks.summary(self.conn, today=today)
        result = wealth.bundle(self.conn, today=today)
        card = next(a for a in result['accounts'] if a['id'] == 'statement:amex')
        self.assertEqual(bank['totals_by_currency'], {'GBP': 450})
        self.assertEqual(result['summary']['net_worth'], 450)
        self.assertEqual(card['as_of'], '2026-09-15')
        self.assertEqual(card['valuation']['reported_source'], 'statement')
        self.assertEqual(next(a for a in planning.account_snapshot(self.conn, today) if a['id'] == 'amex')['balance'], -50)

    def test_later_statement_links_feed_and_retains_review_and_source_identity(self):
        self.account('cash')
        payload = {'transactions': [{'id': 'event-1', 'accountId': 'cash', 'date': '2026-09-12',
                    'amount': -12, 'currency': 'GBP', 'description': 'Synthetic Coffee Shop',
                    'merchant': 'Synthetic Coffee Shop', 'isPending': False}], 'total': 1}
        banks.record_account_result(self.conn, 'cash', {
            'transactions': lunchflow_client.transactions(payload, 'cash'), 'balance': None, 'errors': []
        }, '2026-09-18T09:00:00+00:00')
        row = dict(self.conn.execute('SELECT * FROM transactions').fetchone())
        self.conn.execute("UPDATE transactions SET category='Personal choice',category_source='user' WHERE id=?", (row['id'],))
        statement = ParsedStatement('Synthetic', 'current', 'cash', None, '1234', date(2026, 9, 1),
                                    date(2026, 9, 15), 100, 88,
                                    [RawTxn(date(2026, 9, 7), 'Synthetic Coffee Shop', -12)])
        _store_statement(self.conn, statement, 'synthetic.pdf')
        _store_statement(self.conn, statement, 'synthetic.pdf')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 1)
        after = dict(self.conn.execute('SELECT * FROM transactions').fetchone())
        self.assertEqual(after['id'], row['id'])
        self.assertEqual(after['dedupe_key'], row['dedupe_key'])
        self.assertEqual(after['raw'], row['raw'])
        self.assertEqual(after['category'], 'Personal choice')
        self.assertIsNotNone(after['statement_id'])

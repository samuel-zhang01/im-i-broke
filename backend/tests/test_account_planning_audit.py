"""Financial audit regressions using isolated records and independent SQLite writers."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from app import account_coverage, account_settings, analytics, db, planning, product_imports
from app import lunchflow_client, lunchflow_store


TODAY = date(2026, 9, 22)


class AccountPlanningAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'finance.db'
        self.conn = db.init_db(self.path)
        self.addCleanup(self.conn.close)
        account_coverage.ensure_schema(self.conn)
        lunchflow_store.ensure_schema(self.conn)
        self.conn.commit()

    def account(self, name, currency='GBP', kind='cash'):
        with self.conn:
            return account_settings.create_manual(self.conn, account_settings.ManualAccount(
                display_name=name, currency=currency, account_type=kind))['id']

    def transaction(self, account, amount, currency='GBP', day='2026-08-15', **extra):
        values = dict(account_id=account, amount=amount, currency=currency, txn_date=day,
                      description='Synthetic activity', category='Groceries', merchant='Example shop') | extra
        self.conn.execute('INSERT INTO transactions (' + ','.join(values) + ') VALUES (' + ','.join('?' for _ in values) + ')', tuple(values.values()))

    def coverage(self, account, start='2026-08-01', end='2026-08-31'):
        self.conn.execute('''INSERT INTO account_coverage_periods(account_id,period_start,period_end,complete,source,evidence)
            VALUES(?,?,?,1,'user_confirmed','Synthetic complete history')''', (account, start, end))

    def balance(self, account, amount, day='2026-09-22', currency='GBP'):
        self.conn.execute('INSERT INTO manual_balance_observations(account_id,amount,currency,as_of) VALUES(?,?,?,?)',
                          (account, amount, currency, day))

    def test_currency_save_admits_writer_before_preview_and_import_revalidates(self):
        account = self.account('Concurrent currency')
        body = product_imports.CsvPreview(account_id=account,
            csv_text='date,description,amount\n2026-08-15,Synthetic coffee,-10\n',
            mapping={'date_column': 'date', 'description_column': 'description', 'amount_column': 'amount'})
        prepared = product_imports.prepare(self.conn, body)
        request = product_imports.CsvCommit(**body.model_dump(), preview_token=prepared['preview_token'])
        started, finished = Event(), Event()
        original = account_settings.preview_settings
        def import_later():
            other = db.connect(self.path)
            try:
                started.set()
                with other:
                    other.execute('BEGIN IMMEDIATE')
                    product_imports.commit_import(other, request)
            finally:
                other.close()
                finished.set()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = None
            def preview(conn, *args):
                nonlocal future
                result = original(conn, *args)
                future = pool.submit(import_later)
                self.assertTrue(started.wait(2))
                self.assertFalse(finished.wait(.05), 'Import must wait until the account save commits')
                return result
            with patch.object(account_settings, 'preview_settings', side_effect=preview), self.conn:
                account_settings.save_settings(self.conn, account, {'currency': 'EUR'})
            with self.assertRaises(ValueError):
                future.result(timeout=3)
        self.assertEqual(account_settings.account_view(self.conn, account)['currency'], 'EUR')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 0)

    def test_stale_account_preview_does_not_bypass_new_history(self):
        account = self.account('Prior import')
        self.assertEqual(account_settings.preview_settings(self.conn, account, {'currency': 'EUR'})['blockers'], [])
        with self.conn:
            self.transaction(account, -10)
        with self.assertRaises(ValueError), self.conn:
            account_settings.save_settings(self.conn, account, {'currency': 'EUR'})
        self.assertEqual(account_settings.account_view(self.conn, account)['currency'], 'GBP')
        self.assertEqual(self.conn.execute('SELECT amount FROM transactions').fetchone()[0], -10)

    def test_disjoint_account_writes_keep_name_and_ownership(self):
        account = self.account('Before')
        started, finished = Event(), Event()
        original = account_settings.preview_settings
        def ownership():
            other = db.connect(self.path)
            try:
                started.set()
                with other:
                    return account_settings.save_settings(other, account, {'ownership_share': .5})
            finally:
                other.close()
                finished.set()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = None
            def preview(conn, *args):
                nonlocal future
                result = original(conn, *args)
                if conn is self.conn:
                    future = pool.submit(ownership)
                    self.assertTrue(started.wait(2))
                    self.assertFalse(finished.wait(.05))
                return result
            with patch.object(account_settings, 'preview_settings', side_effect=preview), self.conn:
                account_settings.save_settings(self.conn, account, {'display_name': 'After'})
            future.result(timeout=3)
        account = account_settings.account_view(self.conn, account)
        self.assertEqual((account['display_name'], account['ownership_share']), ('After', .5))

    def test_manual_balance_validation_owns_writer_before_read(self):
        account = self.account('Manual observation')
        original = account_settings.account_view
        attempts = []
        other = db.connect(self.path)
        self.addCleanup(other.close)
        other.execute('PRAGMA busy_timeout=1')
        def view(conn, ident):
            if not attempts:
                import sqlite3
                with self.assertRaises(sqlite3.OperationalError):
                    other.execute('BEGIN IMMEDIATE')
                attempts.append(True)
            return original(conn, ident)
        with patch.object(db, 'DB_PATH', self.path), patch.object(account_settings, 'account_view', side_effect=view):
            response = account_settings.add_manual_balance(account, account_settings.ManualBalance(
                amount=250, currency='GBP', as_of=TODAY))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(account_settings.account_view(self.conn, account)['manual_balance']['amount'], 250)

    def test_foreign_only_and_mixed_coverage_never_certify_zero_spending(self):
        eur = self.account('Euro spending', 'EUR')
        self.transaction(eur, -100, 'EUR')
        self.coverage(eur)
        self.conn.commit()
        result = planning.covered_baseline(self.conn, TODAY)
        self.assertEqual(result['months'], 0)
        self.assertIsNone(result['selected_contribution'])
        self.assertIn('Foreign-currency', next(r['reason'] for r in result['excluded_months'] if r['month']=='2026-08'))
        gbp = self.account('Pound spending')
        self.transaction(gbp, 1000, category='Income')
        self.coverage(gbp)
        self.conn.commit()
        self.assertEqual(planning.covered_baseline(self.conn, TODAY)['months'], 0)
        with self.conn:
            account_settings.save_settings(self.conn, eur, {'include_in_planning': False})
        supported = planning.covered_baseline(self.conn, TODAY)
        self.assertEqual((supported['months'], supported['avg_income']), (1, 1000))

    def test_unnamed_legacy_foreign_account_remains_explicitly_unsupported(self):
        with self.conn:
            self.conn.execute("INSERT INTO accounts(id,bank,account_type,currency) VALUES('legacy-eur','Example','cash','EUR')")
            self.coverage('legacy-eur')
        result = planning.covered_baseline(self.conn, TODAY)
        self.assertEqual(result['months'], 0)
        self.assertIsNone(result['selected_contribution'])
        reason = next(row['reason'] for row in result['excluded_months'] if row['month'] == '2026-08')
        self.assertIn('Foreign-currency', reason)
        self.assertIn('legacy-eur', reason)

    def test_native_foreign_activity_cannot_hide_in_a_gbp_account(self):
        account = self.account('Mixed currency source')
        self.coverage(account)
        raw = lunchflow_client.list_accounts({'accounts': [{'id': 'native', 'name': 'Cash', 'institution_name': 'Manual', 'currency': 'GBP'}]})
        lunchflow_store.discover_accounts(self.conn, raw, lunchflow_store.now())
        lunchflow_store.map_account(self.conn, 'native', account_id=account)
        self.conn.execute('''INSERT INTO bank_transactions(connection_id,provider_id,txn_date,amount,currency,status,raw,first_seen_at,last_seen_at)
            VALUES('native','native-event','2026-08-15',-100,'EUR','native','{}','2026-09-22','2026-09-22')''')
        self.conn.commit()
        result = planning.covered_baseline(self.conn, TODAY)
        self.assertEqual(result['months'], 0)
        self.assertIsNone(result['avg_spending'])
        self.assertEqual(self.conn.execute('SELECT amount,currency FROM bank_transactions').fetchone()[:], (-100, 'EUR'))

    def test_foreign_broker_spending_blocks_baseline_but_trades_and_income_do_not(self):
        bank = self.account('Pound bank')
        broker = self.account('Broker card', kind='investment')
        self.coverage(bank)
        self.transaction(bank, 1000, category='Income')
        self.transaction(broker, -100, 'EUR', category='Groceries')
        result = planning.covered_baseline(self.conn, TODAY)
        self.assertEqual(result['months'], 0)
        self.assertIsNone(result['selected_contribution'])
        self.conn.execute("UPDATE transactions SET category='Investments' WHERE account_id=?", (broker,))
        self.transaction(broker, 100, 'EUR', category='Investment income')
        result = planning.covered_baseline(self.conn, TODAY)
        self.assertEqual((result['months'], result['selected_contribution']), (1, 1000))

    def test_closed_foreign_account_does_not_block_later_supported_months(self):
        eur = self.account('Closed euro account', 'EUR')
        gbp = self.account('Current pounds')
        with self.conn:
            account_settings.save_settings(self.conn, eur, {'closed_on': '2026-07-31'})
            self.coverage(gbp)
            self.transaction(gbp, -25)
        result = planning.covered_baseline(self.conn, TODAY)
        self.assertEqual((result['months'], result['avg_spending']), (1, 25))

    def test_planning_exclusion_removes_cash_credit_investments_and_recurring_inputs(self):
        personal = self.account('Personal cash')
        business = self.account('Business cash')
        card = self.account('Business card', kind='credit')
        investment = self.account('Business portfolio', kind='investment')
        self.balance(personal, 1000)
        self.balance(business, 5000)
        self.balance(card, -250)
        self.balance(investment, 100)
        self.coverage(personal)
        self.transaction(personal, -25)
        for month in ('06', '07', '08'):
            self.transaction(business, -100, day=f'2026-{month}-15', category='Subscriptions')
        self.conn.execute("INSERT INTO transactions(account_id,txn_date,amount,type_code,meta) VALUES(?,'2026-08-01',-100,'Market buy',?)",
                          (investment, json.dumps({'isin':'SYNTHETIC','shares':10})))
        stamp = datetime.now(timezone.utc).isoformat()
        self.conn.execute("INSERT INTO prices(isin,symbol,currency,price_native,price_gbp,quote_asof,updated,identity_verified) VALUES('SYNTHETIC','SYNTHETIC','GBP',20,20,?,?,1)", (stamp,stamp))
        self.conn.execute("INSERT INTO statements(account_id,source_file,period_start,period_end,reconciled) VALUES(?,'excluded.csv','2026-08-01','2026-08-31',0)", (business,))
        self.conn.execute("INSERT INTO transactions(account_id,amount,description) VALUES(?,1,'Excluded undated row')", (business,))
        self.conn.commit()
        original = analytics.household_holdings_value(self.conn)
        self.assertEqual(original['market_value'], 200)
        for ident in (business, card, investment):
            with self.conn:
                account_settings.save_settings(self.conn, ident, {'include_in_planning': False})
        result = planning.planning_bundle(self.conn, today=TODAY)
        self.assertEqual(result['wealth']['known_net_worth'], 1000)
        self.assertEqual(result['wealth']['cash_assets'], 1000)
        self.assertEqual(result['wealth']['liquid_after_debt'], 1000)
        self.assertEqual(result['wealth']['holdings_value'], 0)
        self.assertEqual(result['wealth']['liabilities'], 0)
        self.assertEqual(result['wealth']['investment_cash'], 0)
        self.assertEqual(result['resilience']['recurring_monthly'], 0)
        self.assertEqual(result['quality']['failed_statements'], 0)
        self.assertEqual(result['quality']['undated_transactions'], 0)
        self.assertEqual(result['assumptions']['initial_wealth'], 1000)
        self.assertEqual([a['id'] for a in result['wealth']['accounts']], [personal])
        self.assertEqual(analytics.household_holdings_value(self.conn), original)

    def test_manual_card_uses_final_source_after_old_statement_and_repayment(self):
        account = self.account('Manually tracked card', kind='credit')
        other = self.account('Payment account')
        self.conn.execute("UPDATE accounts SET bank='Example Card' WHERE id=?", (account,))
        self.conn.execute("INSERT INTO statements(account_id,source_file,period_start,period_end,closing_balance,reconciled) VALUES(?,'synthetic.pdf','2026-08-01','2026-08-31',500,1)", (account,))
        self.transaction(other, -500, day='2026-09-05', description='Example Card payment')
        self.balance(account, -250)
        self.conn.commit()
        row = next(a for a in planning.account_snapshot(self.conn, TODAY) if a['id']==account)
        self.assertEqual((row['balance'], row['native_balance'], row['balance_source']), (-250, -250, 'manual'))
        self.assertFalse(row['valuation']['current_unknown'])
        self.assertEqual(row['valuation']['reported_source'], 'manual')
        self.assertEqual(row['valuation']['effective_as_of'], TODAY.isoformat())
        self.assertFalse(row['balance_check']['requires_review'])
        summary = lunchflow_store.summary(self.conn, today=TODAY)
        local = next(a for a in summary['local_accounts'] if a['id']==account)
        self.assertEqual((local['balance_source'],local['balance']),('manual',-250))
        self.assertEqual(summary['statement_liabilities_by_currency']['GBP'],250)
        self.assertNotIn(account,[a['account_id'] for a in summary['balance_checks']])

    def test_older_manual_balance_cannot_replace_a_newer_unknown_statement(self):
        account = self.account('Old observation card', kind='credit')
        other = self.account('Paying account')
        self.conn.execute("UPDATE accounts SET bank='Example Card' WHERE id=?", (account,))
        self.balance(account, -100, day='2026-07-31')
        self.conn.execute("INSERT INTO statements(account_id,source_file,period_start,period_end,closing_balance,reconciled) VALUES(?,'synthetic.pdf','2026-08-01','2026-08-31',500,1)", (account,))
        self.transaction(other,-500,day='2026-09-05',description='Example Card payment')
        row = next(a for a in planning.account_snapshot(self.conn, TODAY) if a['id']==account)
        self.assertEqual(row['balance_source'], 'statement')
        self.assertTrue(row['valuation']['current_unknown'])
        self.assertIsNone(row['balance'])


if __name__ == '__main__':
    unittest.main()

"""The legacy balance axis contains only GBP bank/card balances, never NAV."""
from contextlib import closing as close_connection
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app import main


class TimeseriesScopeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'synthetic.db'
        with close_connection(self.connection()) as conn, conn:
            conn.executescript('''
                CREATE TABLE accounts(id TEXT PRIMARY KEY,display_name TEXT,account_type TEXT,currency TEXT);
                CREATE TABLE statements(id INTEGER PRIMARY KEY,account_id TEXT,period_start TEXT,period_end TEXT,opening_balance REAL,closing_balance REAL);
                CREATE TABLE transactions(id INTEGER PRIMARY KEY,account_id TEXT,txn_date TEXT,amount REAL,balance REAL);
            ''')
        change = patch.object(main, 'get_conn', side_effect=self.connection)
        change.start(); self.addCleanup(change.stop)
        cashflow = patch.object(main.analytics, 'monthly_cashflow', return_value=[])
        cashflow.start(); self.addCleanup(cashflow.stop)

    def connection(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def account(self, id, *, kind='debit', code='GBP', opening=100, closing=100, amount=None):
        with close_connection(self.connection()) as conn, conn:
            conn.execute('INSERT INTO accounts VALUES(?,?,?,?)', (id, id, kind, code))
            conn.execute('INSERT INTO statements(account_id,period_start,period_end,opening_balance,closing_balance) VALUES(?,?,?,?,?)',
                         (id, '2026-09-01', '2026-09-18', opening, closing))
            if amount is not None:
                conn.execute('INSERT INTO transactions(account_id,txn_date,amount,balance) VALUES(?,?,?,NULL)',
                             (id, '2026-09-02', amount))

    def test_investment_cash_and_native_foreign_balances_cannot_enter_gbp_axis(self):
        self.account('cash', closing=125)
        self.account('broker', kind='investment', opening=500000, closing=550000, amount=90000)
        self.account('dollars', code='USD', opening=1000000, closing=1100000)
        result = main.timeseries(interval='day')
        self.assertEqual(result['balances_currency'], 'GBP')
        self.assertEqual(set(result['balances']), {'cash'})
        self.assertEqual(result['balances']['cash'][-1]['balance'], 125)
        self.assertEqual(result['excluded_balance_accounts'], 2)

    def test_known_zero_overdraft_and_card_debt_keep_their_signs(self):
        self.account('zero', opening=0, closing=0)
        self.account('overdraft', opening=-40, closing=-70)
        self.account('card', kind='credit', opening=100, closing=250)
        self.account('overpaid', kind='credit', opening=-10, closing=-20)
        result = main.timeseries(interval='month')
        self.assertEqual({name: points[-1]['balance'] for name, points in result['balances'].items()},
                         {'zero': 0, 'overdraft': -70, 'card': -250, 'overpaid': 20})

    def test_unanchored_transactions_do_not_invent_zero_opening_balance(self):
        self.account('unknown', opening=None, closing=None, amount=500)
        self.account('legacy', code=None, opening=50, closing=None, amount=-5)
        result = main.timeseries(interval='day')
        self.assertNotIn('unknown', result['balances'])
        self.assertEqual(result['balances']['legacy'][-1]['balance'], 45)

    def test_explicit_foreign_or_investment_filter_is_empty_not_mislabelled(self):
        self.account('pounds', closing=500)
        self.account('broker', kind='investment', code='GBP', amount=999)
        self.account('dollars', code='USD')
        for account in ('broker', 'dollars'):
            result = main.timeseries(interval='month', account=account)
            self.assertEqual(result['balances'], {})
            self.assertEqual(result['excluded_balance_accounts'], 1)
        self.assertEqual(set(main.timeseries(interval='day', account='pounds')['balances']), {'pounds'})


if __name__ == '__main__':
    unittest.main()

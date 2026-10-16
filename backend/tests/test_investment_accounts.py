"""Account isolation across positions, cash, turnover, prices and trade drill-down."""
import json
import sqlite3
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from app import analytics
from app.db import SCHEMA
from app.portfolio import holding_positions
from app.portfolio_audit import trade_history
from app.investment_accounts import selected_accounts


class InvestmentAccountsTests(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(':memory:')
        self.c.row_factory = sqlite3.Row
        self.c.executescript(SCHEMA)
        self.now = datetime.now(timezone.utc)
        for account, bank in [('one', 'Trading212'), ('two', 'Moomoo')]:
            self.c.execute('INSERT INTO accounts(id,bank,account_type,display_name) VALUES(?,?,?,?)', (account, bank, 'investment', bank))
        self.event('one', 'Deposit', 0, 1000)
        self.event('one', 'Market buy', 10, -100)
        self.event('two', 'Deposit', 0, 500)
        self.event('two', 'Market buy', 3, -90)
        self.c.execute('INSERT INTO prices(isin,symbol,currency,price_native,price_gbp,identity_verified,quote_asof,fetched_at) VALUES(?,?,?,?,?,?,?,?)', ('US0000000001','TEST','GBP',20,20,1,self.now.isoformat(),self.now.isoformat()))
        self.c.commit()

    def tearDown(self):
        self.c.close()

    def event(self, account, action, shares, amount, **extra):
        meta = {'isin':'US0000000001','ticker':'TEST','shares':shares,'native_currency':'GBP', 'native_amount':amount,'cash_movements':{'GBP':amount}, **extra}
        self.c.execute('INSERT INTO transactions(account_id,txn_date,type_code,amount,meta) VALUES(?,?,?,?,?)', (account,'2026-01-01',action,amount,json.dumps(meta)))

    def test_independent_cost_books_and_filtered_cash_turnover(self):
        one = holding_positions(self.c, ['one'])['US0000000001']
        two = holding_positions(self.c, ['two'])['US0000000001']
        all_positions = holding_positions(self.c)['US0000000001']
        self.assertEqual((one['net_shares'],one['cost_basis']), (10,100))
        self.assertEqual((two['net_shares'],two['cost_basis']), (3,90))
        self.assertEqual((all_positions['net_shares'],all_positions['cost_basis']), (13,190))
        for account, bought, cash in [('one',100,900), ('two',90,410)]:
            data = analytics.investments_bundle(self.c,[account])
            self.assertEqual((data['stats']['invested'],data['stats']['cash_balance']), (bought,cash))
            self.assertEqual(data['selected_account_ids'],[account])
            self.assertEqual(len(data['accounts']),2)
            self.assertEqual(trade_history(self.c,'US0000000001',account_ids=[account])['total'],1)

    def test_price_refresh_and_foreign_cash_only_use_selected_accounts(self):
        self.event('one','Deposit',0,0,cash_movements={'USD':50})
        self.event('two','Deposit',0,0,cash_movements={'USD':90})
        with patch('app.prices.batch_quote', return_value={}) as quotes:
            data = analytics.valuation(self.c,now=self.now,account_ids=['two'])
            self.assertEqual(data['totals']['market_value'],60)
            self.assertEqual(data['totals']['foreign_cash'][0]['amount'],90)
            self.assertEqual(data['holdings'][0]['account_names'],['Moomoo'])
            quotes.assert_not_called()
        self.assertEqual(analytics._foreign_cash(self.c,self.now,['one'])[0]['amount'],50)

    def test_no_selection_is_not_all_and_unknown_ids_are_rejected(self):
        self.assertEqual(selected_accounts(self.c,['__none__']),[])
        self.assertEqual(holding_positions(self.c,[]),{})
        self.assertEqual(analytics.investments_bundle(self.c,[])['stats']['cash_balance'],0)
        with patch('app.prices.batch_quote') as quotes:
            self.assertEqual(analytics.valuation(self.c,account_ids=[])['holdings'],[])
            quotes.assert_not_called()
        with self.assertRaises(ValueError):
            selected_accounts(self.c,['not-an-investment'])

    def test_gift_cost_unknown_and_split_preserves_cost_and_turnover(self):
        self.event('two','Stock split',9,0,split_ratio=4)
        self.event('two','Stock gift',.1,0)
        p=holding_positions(self.c,['two'])['US0000000001']
        self.assertEqual((p['net_shares'],p['bought_shares'],p['corporate_action_shares'],p['gift_shares']),(12.1,3,9,.1))
        self.assertIsNone(p['cost_basis'])
        self.assertIsNone(p['estimated_cost_basis'])
        self.assertEqual(p['bought'],90)
        self.assertEqual(p['trades'],1)
        self.assertEqual(analytics.investments_bundle(self.c,['two'])['stats']['invested'],90)

    def test_inconsistent_split_marks_position_incomplete(self):
        self.event('two','Stock split',99,0,split_ratio=4)
        self.assertFalse(holding_positions(self.c,['two'])['US0000000001']['position_complete'])

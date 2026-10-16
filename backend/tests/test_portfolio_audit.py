"""The portfolio drill-down preserves source units, currency and fill grain."""
import json
import sqlite3
import unittest
from app.db import SCHEMA
from app.portfolio_audit import trade_history


class PortfolioAuditTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT INTO accounts(id,bank,account_type) VALUES('invest','Trading212','investment')")
        self.conn.execute("INSERT INTO statements(id,account_id,source_file) VALUES(1,'invest','/private/source/old.csv')")

    def tearDown(self):
        self.conn.close()

    def fill(self, date, action, meta, amount=0):
        self.conn.execute("INSERT INTO transactions(account_id,statement_id,txn_date,type_code,amount,meta) VALUES('invest',1,?,?,?,?)",
                          (date, action, amount, json.dumps(meta)))

    def test_pence_and_overlapping_provenance_remain_one_fill(self):
        self.fill('2026-09-15', 'Market buy', {
            'isin': 'IE00B53HP851', 'ticker': 'CUKX', 'shares': 56.5684019,
            'time': '2026-09-15 11:47:14', 'time_timezone': 'UTC',
            'execution_price': 21995, 'execution_price_currency': 'GBX',
            'execution_price_major': 219.95, 'execution_currency_major': 'GBP',
            'native_amount': -12442.22, 'native_currency': 'GBP', 'gbp_value_known': True,
            'exchange_rate': 100, 'provider_id': 'DO-NOT-PUBLISH',
            'source_observations': [{'source_file': '/private/source/new.csv'}, {'source_file': '/private/source/old.csv'}],
        }, -12442.22)
        result = trade_history(self.conn, 'IE00B53HP851')
        self.assertEqual(result['total'], 1)
        fill = result['trades'][0]
        self.assertEqual((fill['price'], fill['price_currency'], fill['price_major']), (21995, 'GBX', 219.95))
        self.assertAlmostEqual(abs(fill['shares'] * fill['price_major']), abs(fill['cash_amount']), places=2)
        self.assertEqual(fill['sources'], ['new.csv', 'old.csv'])
        self.assertNotIn('/private', json.dumps(result))
        self.assertNotIn('DO-NOT-PUBLISH', json.dumps(result))

    def test_foreign_cash_is_not_relabelled_gbp_and_paging_is_stable(self):
        for hour, shares in [('10', 1), ('11', -0.5), ('12', 2)]:
            self.fill('2026-09-15', 'Market buy' if shares > 0 else 'Market sell', {
                'isin': 'US0000000001', 'time': f'2026-09-15 {hour}:00:00', 'shares': shares,
                'native_currency': 'USD', 'native_amount': -shares * 10, 'gbp_value_known': False,
            })
        self.fill('2026-09-15', 'Dividend', {'isin': 'US0000000001'})
        self.fill('2026-09-15', 'Market buy', {'isin': 'US0000000002'})
        result = trade_history(self.conn, 'US0000000001', limit=1, offset=1)
        self.assertEqual(result['total'], 3)
        self.assertEqual(result['trades'][0]['shares'], -0.5)
        self.assertEqual(result['trades'][0]['cash_amount'], 5)
        self.assertIsNone(result['trades'][0]['gbp_cash_amount'])
        self.assertEqual(trade_history(self.conn, 'US0000000001', offset=999)['trades'], [])


if __name__ == '__main__':
    unittest.main()

class PortfolioWealthIntegrationTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        # Functional API fixtures use local mode; auth/security have dedicated suites.
        self.enterContext(patch.dict('os.environ', {'FINANCE_AUTH_ENABLED': 'false', 'FINANCE_DEPLOYMENT': 'local'}))
        from fastapi.testclient import TestClient
        from datetime import datetime, timezone
        from app import main
        from app.db import init_db
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'portfolio.db'
        self.conn = init_db(self.path)
        self.conn.execute("INSERT INTO accounts(id,bank,account_type) VALUES('invest','Trading212','investment')")
        for amount, cash in [(100, {'GBP': 100}), (0, {'USD': 10}), (0, {'EUR': -5})]:
            self.conn.execute("INSERT INTO transactions(account_id,txn_date,type_code,amount,meta) VALUES('invest','2026-09-15','Deposit',?,?)", (amount, json.dumps({'cash_movements': cash})))
        stamp = datetime.now(timezone.utc).isoformat()
        for currency, rate in [('USD', .8), ('EUR', 1)]:
            self.conn.execute('INSERT INTO fx_quotes(currency,rate_to_gbp,asof,fetched_at) VALUES(?,?,?,?)', (currency, rate, stamp, stamp))
        self.conn.commit()
        self.patcher = patch.object(main, 'DB_PATH', self.path)
        self.patcher.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.patcher.stop()
        self.conn.close()
        self.temp.cleanup()

    def test_foreign_assets_and_debt_enter_wealth_exactly_once(self):
        summary = self.client.get('/api/summary').json()
        plan = self.client.get('/api/planning').json()['wealth']
        self.assertEqual(summary['foreign_cash_value'], 3)
        self.assertEqual(summary['cash_total'], 103)
        self.assertEqual(summary['net_worth'], 103)
        self.assertEqual(plan['known_net_worth'], 103)
        self.assertEqual(plan['investment_cash'], 108)
        self.assertEqual(plan['liabilities'], 5)
        self.assertEqual(plan['investment_cash'] - plan['liabilities'], plan['known_net_worth'])
        # A cached-rate change propagates to both readers without editing cash.
        self.conn.execute("UPDATE fx_quotes SET rate_to_gbp=.9 WHERE currency='USD'")
        self.conn.commit()
        self.assertEqual(self.client.get('/api/summary').json()['net_worth'], 104)
        self.assertEqual(self.client.get('/api/planning').json()['wealth']['known_net_worth'], 104)
        self.assertEqual(self.conn.execute('SELECT SUM(amount) FROM transactions').fetchone()[0], 100)

    def test_invalid_trade_filters_are_rejected(self):
        for params in [{'isin': '../invalid'}, {'isin': 'IE00B53HP851', 'offset': -1}, {'isin': 'IE00B53HP851', 'limit': 9999}]:
            self.assertEqual(self.client.get('/api/investments/trades', params=params).status_code, 422)
        result = self.client.get('/api/investments/trades', params={'isin': 'IE00B53HP851'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['total'], 0)

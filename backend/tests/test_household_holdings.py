"""Household investment choices do not alter original portfolio evidence."""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import account_settings, analytics, db, planning


class HouseholdHoldingsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'test.db'
        self.conn = db.init_db(self.path)
        self.addCleanup(self.conn.close)
        self.now = datetime.now(timezone.utc)
        for account in ('personal', 'joint', 'archive'):
            self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES(?,'Broker','investment',?)", (account, account))
            self.conn.execute('''INSERT INTO statements(account_id,source_file,period_start,period_end,closing_balance,reconciled)
                VALUES(?,?,?, ?,0,1)''', (account, account + '.csv', '2026-01-01', self.now.date().isoformat()))
        self.trade('personal', 10, -100, 'COMMON')
        self.trade('joint', 20, -300, 'COMMON')
        self.trade('joint', -5, 100, 'COMMON', day='2026-01-02')
        self.trade('archive', 5, -50, 'MISSING')
        for account, amount in (('personal', 100), ('joint', 200), ('archive', 50)):
            self.cash(account, 'GBP', amount)
        self.cash('personal', 'USD', 20)
        self.cash('joint', 'USD', 100)
        self.cash('archive', 'EUR', 40)
        self.price('COMMON', 20, self.now)
        self.conn.execute("INSERT INTO fx_quotes(currency,rate_to_gbp,asof) VALUES('USD',.8,?)", (self.now.isoformat(),))
        self.conn.commit()

    def trade(self, account, shares, amount, isin, day='2026-01-01', **metadata):
        self.conn.execute('''INSERT INTO transactions(account_id,txn_date,amount,type_code,meta)
            VALUES(?,?,?,?,?)''', (account, day, amount, 'Market buy' if shares > 0 else 'Market sell',
            json.dumps({'isin': isin, 'shares': shares, **metadata})))

    def cash(self, account, currency, amount):
        self.conn.execute('''INSERT INTO transactions(account_id,txn_date,amount,meta) VALUES(?,'2026-01-01',?,?)''',
                          (account, amount if currency == 'GBP' else 0, json.dumps({'cash_movements': {currency: amount}})))

    def price(self, isin, value, stamp):
        self.conn.execute('''INSERT INTO prices(isin,symbol,currency,price_native,price_gbp,quote_asof,updated,identity_verified)
            VALUES(?,?,'GBP',?,?,?,?,1)''', (isin, isin, value, value, stamp.isoformat(), stamp.isoformat()))

    def choices(self, *, archive=True):
        account_settings.save_settings(self.conn, 'joint', {'ownership_share': .5})
        account_settings.save_settings(self.conn, 'archive', {'archived': archive})
        self.conn.commit()

    def test_absent_and_default_settings_preserve_every_valuation_field(self):
        original = analytics.holdings_value(self.conn, self.now)
        self.assertEqual(analytics.household_holdings_value(self.conn, self.now), original)
        account_settings.save_settings(self.conn, 'joint', {'display_name': 'Renamed ISA', 'ownership_share': 1})
        self.assertEqual(analytics.household_holdings_value(self.conn, self.now), original)

    def test_exclusion_without_fraction_preserves_existing_scoped_valuation(self):
        account_settings.save_settings(self.conn, 'archive', {'include_in_reports': False})
        self.assertEqual(analytics.household_holdings_value(self.conn, self.now),
                         analytics.holdings_value(self.conn, self.now, ['personal', 'joint']))

    def test_weighted_cost_profit_and_foreign_cash_leave_source_portfolio_unchanged(self):
        original = analytics.holdings_value(self.conn, self.now)
        evidence = [dict(row) for row in self.conn.execute('SELECT * FROM transactions')]
        self.choices()
        total = analytics.household_holdings_value(self.conn, self.now)
        self.assertEqual((total['market_value'], total['cost_basis'], total['unrealised'], total['realised'], total['total_pl']),
                         (350, 212.5, 137.5, 12.5, 150))
        self.assertEqual((total['estimated_unrealised'], total['estimated_realised']), (137.5, 12.5))
        self.assertEqual((total['foreign_cash_value'], total['foreign_cash_unvalued']), (56, 0))
        self.assertEqual([(row['currency'], row['amount']) for row in total['foreign_cash']], [('USD', 70)])
        self.assertEqual(total['native_totals'][0]['known_market_value'], 350)
        self.assertEqual(analytics.holdings_value(self.conn, self.now), original)
        self.assertEqual([dict(row) for row in self.conn.execute('SELECT * FROM transactions')], evidence)

    def test_missing_quotes_cost_and_fx_remain_explicit_and_oldest_price_wins(self):
        self.choices(archive=False)
        self.trade('joint', 2, 0, 'NATIVE', native_currency='EUR', native_amount=-100, cost_basis_gbp=None)
        old = self.now - timedelta(days=2)
        self.price('NATIVE', 30, old)
        self.conn.execute("UPDATE fx_quotes SET asof=?", (old.isoformat(),))
        total = analytics.household_holdings_value(self.conn, self.now)
        self.assertEqual((total['n_unpriced'], total['n_stale'], total['n_unknown_cost']), (1, 1, 1))
        self.assertIsNone(total['cost_basis'])
        self.assertIsNone(total['unrealised'])
        self.assertFalse(total['valuation_complete'])
        self.assertEqual((total['asof'], total['newest_asof']), (old.isoformat(), self.now.isoformat()))
        self.assertEqual((total['foreign_cash_unvalued'], total['foreign_cash_stale']), (1, 1))

    def test_incomplete_position_remains_partial_after_ownership_projection(self):
        self.choices()
        self.trade('joint', -1, 10, 'OPENING_UNKNOWN')
        total = analytics.household_holdings_value(self.conn, self.now)
        self.assertFalse(total['position_complete'])
        self.assertFalse(total['valuation_complete'])
        self.assertIsNone(total['realised'])

    def test_summary_and_planning_use_household_values_and_ignore_excluded_quality(self):
        from app import main
        self.choices()
        self.conn.execute("UPDATE statements SET reconciled=0,period_end='2020-01-01' WHERE account_id='archive'")
        self.conn.commit()
        with patch.object(main, 'get_conn', side_effect=lambda: db.connect(self.path)):
            summary = main.summary()
        self.assertEqual((summary['holdings_value'], summary['foreign_cash_value'], summary['net_worth']), (350, 56, 406))
        self.assertFalse(summary['wealth_partial'])
        result = planning.planning_bundle(self.conn, today=self.now.date())['wealth']
        self.assertEqual((result['holdings_value'], result['foreign_cash_value'], result['known_net_worth']), (350, 56, 406))
        self.assertFalse(result['is_partial'])


if __name__ == '__main__':
    unittest.main()

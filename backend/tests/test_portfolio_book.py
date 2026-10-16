"""Portfolio conservation, native units and explicitly estimated historical FX."""
import json
import sqlite3
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from app import analytics
from app.db import SCHEMA


class PortfolioBookTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS reference_fx(date TEXT,currency TEXT,rate_to_gbp REAL,
                source TEXT,observed_date TEXT,fetched_at TEXT,source_url TEXT,PRIMARY KEY(date,currency));
            CREATE TABLE IF NOT EXISTS fx_quotes(currency TEXT PRIMARY KEY,rate_to_gbp REAL,
                asof TEXT,fetched_at TEXT,source_url TEXT);
            INSERT INTO accounts(id,bank,account_type) VALUES('invest','Test','investment');
        ''')
        self.now = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)

    def tearDown(self):
        self.conn.close()

    def trade(self, shares, amount, day='2026-01-01', isin='TEST', **meta):
        self.conn.execute('INSERT INTO transactions(account_id,txn_date,merchant,type_code,amount,meta) VALUES(?,?,?,?,?,?)',
                          ('invest', day, isin, 'Market buy' if shares > 0 else 'Market sell', amount,
                           json.dumps({'isin': isin, 'shares': shares, **meta})))

    def position(self, isin='TEST'):
        return analytics._holding_positions(self.conn)[isin]

    def reference(self, day, multiplier, currency='USD'):
        self.conn.execute('INSERT INTO reference_fx(date,currency,rate_to_gbp,source,observed_date,fetched_at) VALUES(?,?,?,?,?,?)',
                          (day, currency, multiplier, 'ECB reference via Frankfurter', day, self.now.isoformat()))

    def test_partial_disposal_includes_total_fees_once_and_separates_reported_result(self):
        self.trade(10, -101, fees=[{'name': 'fee', 'amount': 1, 'currency': 'GBP'}])
        self.trade(-4, 59, '2026-02-01', result=20, result_currency='GBP', fees=[{'name': 'fee', 'amount': 1, 'currency': 'GBP'}])
        p = self.position()
        self.assertEqual((p['net_shares'], p['cost_basis'], p['realised']), (6, 60.6, 18.6))
        self.assertEqual(p['reported_results'], [{'currency': 'GBP', 'amount': 20}])
        self.assertEqual((p['bought'], p['sold']), (101, 59))
        self.assertAlmostEqual(p['bought'] - p['sold'], p['cost_basis'] - p['realised'])

    def test_pence_execution_does_not_multiply_gbp_cost_by_100(self):
        self.trade(56.5684019, -12442.22, native_currency='GBP', native_amount=-12442.22,
                   execution_price=21995, execution_price_currency='GBX', execution_currency_major='GBP',
                   execution_price_major=219.95, exchange_rate=100, cost_basis_gbp=12442.22)
        p = self.position()
        self.assertEqual((p['native_currency'], p['cost_basis'], p['native_cost_basis']), ('GBP', 12442.22, 12442.22))
        self.assertEqual(p['net_shares'], 56.5684019)

    def test_native_book_translates_printed_total_currency_with_trade_rate(self):
        self.trade(10, -81, native_currency='GBP', native_amount=-81, execution_price_currency='USD', exchange_rate=1.25)
        self.trade(-4, 0, '2026-02-01', native_currency='USD', native_amount=59, execution_price_currency='USD', exchange_rate=1, proceeds_gbp=None)
        p = self.position()
        self.assertEqual(p['native_currency'], 'USD')
        self.assertEqual(p['native_cost_basis'], 60.75)
        self.assertEqual(p['native_realised'], 18.5)
        self.assertIsNone(p['realised'])
        self.assertEqual(p['cost_basis'], 48.6)

    def test_reference_fx_estimate_never_becomes_actual_cash_or_exact_cost(self):
        self.trade(10, 0, native_currency='USD', native_amount=-100, cost_basis_gbp=None)
        self.trade(-4, 0, '2026-02-01', native_currency='USD', native_amount=60, proceeds_gbp=None)
        self.reference('2026-01-01', .8)
        self.reference('2026-02-01', .75)
        p = self.position()
        self.assertIsNone(p['cost_basis'])
        self.assertIsNone(p['realised'])
        self.assertEqual((p['native_cost_basis'], p['native_realised']), (60, 20))
        self.assertEqual((p['estimated_cost_basis'], p['estimated_realised']), (48, 13))
        self.assertEqual((p['estimated_bought'], p['estimated_sold']), (80, 45))
        self.assertEqual(p['estimated_trade_count'], 2)
        self.assertEqual(self.conn.execute('SELECT SUM(amount) FROM transactions').fetchone()[0], 0)
        prices = {'TEST': {'price_gbp': 12, 'price_native': 16, 'currency': 'USD',
                          'identity_verified': 1, 'quote_asof': self.now.isoformat(), 'fx_asof': self.now.isoformat()}}
        totals = analytics._valuation_totals({'TEST': p}, prices, self.now)
        self.assertEqual((totals['estimated_unrealised'], totals['estimated_realised'], totals['estimated_total_pl']), (24, 13, 37))
        self.assertIsNone(totals['total_pl'])
        self.assertEqual(totals['native_totals'][0]['unrealised'], 36)
        self.assertEqual((totals['estimated_unrealised_pct'], totals['n_estimated_cost']), (50, 1))

    def test_missing_dated_reference_is_not_filled_with_current_fx(self):
        self.trade(10, 0, native_currency='USD', native_amount=-100, cost_basis_gbp=None)
        self.reference('2026-01-02', .8)
        self.conn.execute("INSERT INTO fx_quotes(currency,rate_to_gbp,asof) VALUES('USD',.75,?)", (self.now.isoformat(),))
        self.assertIsNone(self.position()['estimated_cost_basis'])

    def test_closed_unknown_lot_allows_new_known_basis_but_realised_stays_incomplete(self):
        self.trade(10, 0, native_currency='USD', native_amount=-100, cost_basis_gbp=None)
        self.trade(-10, 150, '2026-02-01')
        self.trade(5, -200, '2026-03-01')
        p = self.position()
        self.assertEqual((p['net_shares'], p['cost_basis'], p['avg_cost']), (5, 200, 40))
        self.assertIsNone(p['realised'])

    def test_current_foreign_cash_value_is_separate_from_security_market_value(self):
        self.conn.execute("INSERT INTO transactions(account_id,txn_date,amount,meta) VALUES('invest','2026-01-01',0,?)",
                          (json.dumps({'cash_movements': {'USD': 100}}),))
        self.conn.execute("INSERT INTO fx_quotes(currency,rate_to_gbp,asof,source_url) VALUES('USD',.75,?,'https://example.test/fx')", (self.now.isoformat(),))
        total = analytics.holdings_value(self.conn, now=self.now)
        self.assertEqual((total['market_value'], total['foreign_cash_value'], total['foreign_cash_unvalued'], total['foreign_cash_stale']), (0, 75, 0, 0))

    def test_unverified_legacy_quote_does_not_count_as_current_price(self):
        self.trade(10, -100)
        old = {'price_gbp': 99, 'price_native': 99, 'currency': 'GBP', 'updated': self.now.isoformat(), 'identity_verified': 0}
        totals = analytics._valuation_totals({'TEST': self.position()}, {'TEST': old}, self.now)
        self.assertEqual((totals['n_unpriced'], totals['market_value']), (1, 0))

    def test_fetch_timestamp_cannot_make_undated_quote_or_fx_fresh(self):
        quote = {'identity_verified': 1, 'currency': 'USD', 'price_gbp': 10,
                 'fetched_at': self.now.isoformat(), 'updated': self.now.isoformat()}
        self.assertTrue(analytics._price_stale(quote, self.now))
        quote['quote_asof'] = self.now.isoformat()
        self.assertTrue(analytics._price_stale(quote, self.now))
        quote['fx_asof'] = self.now.isoformat()
        self.assertFalse(analytics._price_stale(quote, self.now))

    def test_old_market_close_recently_fetched_does_not_refetch_on_every_read(self):
        self.trade(10, -100)
        self.conn.execute('INSERT INTO prices(isin,symbol,currency,price_native,price_gbp,updated,fetched_at,quote_asof,identity_verified) '
                          'VALUES(?,?,?,?,?,?,?,?,?)', ('TEST', 'TEST.L', 'GBP', 15, 15, self.now.isoformat(),
                          self.now.isoformat(), '2026-09-14T15:30:00+00:00', 1))
        with patch('app.prices.batch_quote') as fetch:
            result = analytics.valuation(self.conn, now=self.now)
        fetch.assert_not_called()
        self.assertEqual(result['totals']['market_value'], 150)
        self.assertTrue(result['holdings'][0]['stale'])
        self.assertEqual(result['holdings'][0]['asof'], '2026-09-14T15:30:00+00:00')

    def test_headline_market_value_and_gains_sum_the_displayed_fractional_holdings(self):
        prices = {}
        for isin in ('A', 'B', 'C'):
            self.trade(1.23456789, -10, isin=isin)
            prices[isin] = {'identity_verified': 1, 'price_gbp': 12.3456, 'price_native': 12.3456,
                            'currency': 'GBP', 'quote_asof': self.now.isoformat()}
        positions = analytics._holding_positions(self.conn)
        rows = [analytics._position_view(p, prices[p['isin']], self.now) for p in positions.values()]
        totals = analytics._valuation_totals(positions, prices, self.now)
        self.assertEqual(totals['market_value'], round(sum(row['market_value'] for row in rows), 2))
        self.assertEqual(totals['unrealised'], round(totals['market_value'] - totals['cost_basis'], 2))
        self.assertEqual(totals['estimated_unrealised'], round(totals['market_value'] - totals['estimated_cost_basis'], 2))
        self.assertEqual(totals['native_totals'][0]['market_value'], totals['market_value'])

    def test_missing_native_quote_keeps_unknown_total_separate_from_known_subtotal(self):
        self.trade(10, 0, native_currency='USD', native_amount=-100, cost_basis_gbp=None)
        group = analytics._valuation_totals(analytics._holding_positions(self.conn), {}, self.now)['native_totals'][0]
        self.assertIsNone(group['market_value'])
        self.assertIsNone(group['unrealised'])
        self.assertEqual((group['known_market_value'], group['n_unpriced'], group['cost_basis']), (0, 1, 100))

    def event(self, action, amount, native_amount=None, currency='GBP', day='2026-01-01'):
        native_amount = amount if native_amount is None else native_amount
        self.conn.execute('INSERT INTO transactions(account_id,txn_date,type_code,amount,meta) VALUES(?,?,?,?,?)',
                          ('invest', day, action, amount, json.dumps({'native_currency': currency,
                          'native_amount': native_amount, 'cash_movements': {currency: native_amount},
                          'gbp_value_known': currency == 'GBP'})))

    def test_lifetime_estimates_do_not_erase_exact_income_or_change_ledger(self):
        self.trade(10, -100)
        self.trade(10, 0, '2026-02-01', native_currency='USD', native_amount=-200,
                   cash_movements={'USD': -200}, cost_basis_gbp=None)
        self.trade(-5, 80, '2026-03-01')
        self.event('Dividend (Ordinary)', 5)
        self.event('Card credit', 40)  # spending refund, never investment income
        self.reference('2026-02-01', .75)
        bundle = analytics.investments_bundle(self.conn)
        stats = bundle['stats']
        self.assertIsNone(stats['invested'])
        self.assertEqual((stats['estimated_invested'], stats['divested'], stats['estimated_net_invested']), (250, 80, 170))
        self.assertEqual((stats['income'], stats['cashback'], stats['cash_balance']), (5, 0, 25))
        native = {row['currency']: row for row in bundle['native_currency_totals']}
        self.assertEqual((native['USD']['invested'], native['USD']['cash_balance']), (200, -200))
        month = next(row for row in bundle['timeline'] if row['month'] == '2026-02')
        self.assertEqual((month['bought'], month['estimated_bought'], month['cash_history_complete']), (0, 150, False))
        self.assertEqual(bundle['holdings'][0]['estimated_net'], 170)

    def test_missing_income_fx_keeps_other_lifetime_metrics_available(self):
        self.trade(10, -100)
        self.event('Dividend (Ordinary)', 0, native_amount=10, currency='USD')
        stats = analytics.investments_bundle(self.conn)['stats']
        self.assertEqual((stats['invested'], stats['net_invested']), (100, 100))
        self.assertIsNone(stats['income'])
        self.assertIsNone(stats['estimated_income'])
        self.reference('2026-01-01', .75)
        stats = analytics.investments_bundle(self.conn)['stats']
        self.assertEqual(stats['estimated_income'], 7.5)
        self.assertIsNone(stats['income'])

    def test_refresh_collects_income_and_deposit_fx_without_converting_principal(self):
        self.event('Dividend (Ordinary)', 0, 10, 'USD', '2026-01-02')
        self.event('Deposit', 0, 100, 'EUR', '2026-01-03')
        self.event('Currency conversion', 0, -1, 'USD', '2026-01-04')
        api = Mock()
        api.batch_reference_fx.return_value = {}
        api.current_fx_to_gbp.return_value = None
        result = analytics._refresh_fx(self.conn, api, self.now)
        api.batch_reference_fx.assert_called_once_with([('EUR', '2026-01-03'), ('USD', '2026-01-02')])
        self.assertEqual(result['historical_attempted'], 2)


if __name__ == '__main__':
    unittest.main()

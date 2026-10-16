"""Regressions for cash-flow classification and honest investment valuation."""
import json
import sqlite3
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from app import analytics, prices
from app.db import SCHEMA


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        for aid, bank, kind in [('bank', 'HSBC', 'current'), ('other', 'Revolut', 'current'),
                                ('card', 'American Express', 'credit'), ('invest', 'Trading212', 'investment')]:
            self.conn.execute('INSERT INTO accounts(id,bank,account_type,display_name) VALUES(?,?,?,?)',
                              (aid, bank, kind, aid))

    def tearDown(self):
        self.conn.close()

    def txn(self, amount, day='2026-01-05', account='bank', merchant='Shop',
            category='Shopping', description=None, code=None, source='auto', meta=None, group=None):
        return self.conn.execute(
            'INSERT INTO transactions(account_id,txn_date,amount,merchant,description,category,'
            'type_code,category_source,meta,transfer_group) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (account, day, amount, merchant, description or merchant, category, code, source,
             json.dumps(meta) if meta else None, group)).lastrowid

    def trade(self, shares, amount, day, isin='GBTEST', account='invest', **meta):
        self.txn(amount, day, account, 'TEST', 'Investments',
                 code='Market buy' if shares > 0 else 'Market sell',
                 meta={'isin': isin, 'shares': shares, **meta})

    def price(self, isin, price, updated):
        self.conn.execute('INSERT INTO prices(isin,symbol,currency,price_native,price_gbp,updated,quote_asof,identity_verified) VALUES(?,?,?,?,?,?,?,1)',
                          (isin, 'TEST.L', 'GBP', price, price, updated, updated))

    def test_biggest_expense_and_category_matrix_reconcile(self):
        self.txn(-2)
        self.txn(-900, category=None)
        self.txn(2000, category='Income')
        bundle = analytics.analytics_bundle(self.conn)
        self.assertEqual(bundle['stats']['biggest_expense']['amount'], 900)
        self.assertEqual(sum(sum(s['data']) for s in bundle['category_months']['series']), 902)

    def test_refunds_reduce_spending_without_inflating_income(self):
        self.txn(-100, account='card')
        self.txn(20, account='card', category='Income', description='Merchant return')
        self.txn(10, description='Purchase refund')
        self.txn(1000, category='Income')
        month = analytics.monthly_cashflow(self.conn)[0]
        self.assertEqual((month['income'], month['spend'], month['refunds'], month['net']), (1000, 70, 30, 930))
        self.assertEqual(analytics.monthly_cashflow(self.conn, account='card', interval='day')[0]['spend'], 80)

    def test_missing_calendar_month_is_not_omitted_from_average(self):
        self.txn(-30, day='2026-01-10')
        self.txn(-60, day='2026-03-10')
        months = analytics.monthly_cashflow(self.conn)
        self.assertEqual([m['m'] for m in months], ['2026-01', '2026-02', '2026-03'])
        self.assertEqual(analytics.analytics_bundle(self.conn)['stats']['avg_monthly_spend'], 30)
        self.assertIsNone(analytics.analytics_bundle(self.conn)['stats']['savings_rate'])

    def test_same_amount_salary_and_purchase_do_not_become_transfer(self):
        self.txn(-100, description='Store purchase')
        self.txn(100, account='other', description='Salary', category='Income')
        self.assertEqual(analytics.detect_transfers(self.conn), [])

    def test_card_repayment_is_linked_without_mutating_categories(self):
        out = self.txn(-100, description='American Express', code='DD', category='Uncategorized')
        inc = self.txn(100, account='card', description='PAYMENT RECEIVED - THANK YOU', category='Transfers & payments')
        result = analytics.recompute_all(self.conn)
        self.assertEqual(result['transfers_linked'], 2)
        self.assertEqual(self.conn.execute('SELECT category FROM transactions WHERE id=?', (out,)).fetchone()[0], 'Uncategorized')
        self.assertEqual(analytics.monthly_cashflow(self.conn), [])

    def test_ambiguous_transfer_is_left_unmatched(self):
        self.txn(-100, description='Revolut self transfer', code='BP')
        for _ in range(2):
            self.txn(100, account='other', description='HSBC self transfer', code='FPI')
        self.assertEqual(analytics.detect_transfers(self.conn), [])

    def test_user_spending_category_takes_precedence_over_transfer_heuristic(self):
        self.txn(-100, description='Revolut self transfer', code='BP', source='user')
        self.txn(100, account='other', description='HSBC self transfer', code='FPI')
        self.assertEqual(analytics.detect_transfers(self.conn), [])

    def test_recurring_excludes_transfer_and_detects_ended_with_newer_coverage(self):
        for day in ['2026-01-01', '2026-02-01', '2026-03-01']:
            self.txn(-10, day=day, merchant='Stream', category='Subscriptions')
            self.txn(-100, day=day, merchant='Transfer', category='Transfers & payments')
        self.txn(-1, day='2026-06-15', merchant='Other')
        rows = analytics.detect_recurring(self.conn, as_of=date(2026, 6, 15))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'ended')
        self.assertEqual(rows[0]['monthly_cost'], 0)

    def test_stale_export_does_not_prove_cancellation(self):
        for day in ['2025-01-01', '2025-02-01', '2025-03-01']:
            self.txn(-10, day=day, merchant='Stream', category='Subscriptions')
        rec = analytics.detect_recurring(self.conn, as_of=date(2026, 1, 1))[0]
        self.assertEqual(rec['status'], 'active')
        self.assertTrue(rec['data_stale'])
        self.assertEqual(analytics.savings_advice(self.conn), [])

    def test_anomaly_not_hidden_by_its_own_standard_deviation(self):
        for i, amount in enumerate([-10, -10, -10, -10, -10, -1000]):
            self.txn(amount, day=f'2026-01-{1 + i * 4:02d}', merchant='Same shop')
        anomalies = analytics.detect_anomalies(self.conn)
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]['amount'], -1000)

    def test_duplicate_candidates_stay_within_account(self):
        self.txn(-10)
        self.txn(-10, account='other')
        self.assertEqual(analytics.detect_anomalies(self.conn), [])

    def test_people_reimbursements_and_exclusions_reconcile(self):
        friend_out = self.txn(-50, merchant='Friend', category='Friends & shared costs')
        friend_in = self.txn(20, merchant='Friend', category='Friends & shared costs')
        refund = self.txn(30, merchant='Friend', category='Friends & shared costs')
        self.conn.execute("UPDATE transactions SET merchant_kind='person' WHERE id IN (?,?,?)", (friend_out, friend_in, refund))
        self.conn.execute("UPDATE transactions SET spending_treatment='reimbursement' WHERE id=?", (refund,))
        self.txn(-100)
        own_out, own_in = self.txn(-500), self.txn(500, account='other')
        self.conn.execute("UPDATE transactions SET spending_treatment='self_transfer' WHERE id IN (?,?)", (own_out, own_in))
        hidden_out, hidden_in = self.txn(-10), self.txn(15, category='Income')
        self.conn.execute("UPDATE transactions SET exclude_from_analysis=1 WHERE id IN (?,?)", (hidden_out, hidden_in))
        result = analytics.analytics_bundle(self.conn)
        self.assertEqual((result['stats']['total_income'], result['stats']['gross_spend'], result['stats']['refunds'], result['stats']['total_spend']), (20, 150, 30, 120))
        excluded = {r['reason']: r for r in result['exclusions']}
        self.assertEqual((excluded['Self transfers']['outflow'], excluded['Self transfers']['inflow']), (500, 500))
        self.assertEqual((excluded['User exclusions']['outflow'], excluded['User exclusions']['income_impact']), (10, 15))
        self.assertEqual(sum(r['spend'] for r in result['by_account']), 120)
        self.assertEqual(sum(r['spend'] for r in result['category_totals']), 120)

    def test_optional_outlier_filter_retains_recorded_totals_and_refunds(self):
        for i, amount in enumerate([-10, -10, -10, -10, -10, -1000]):
            self.txn(amount, day=f'2026-01-{1+i*4:02d}', merchant='Same shop')
        self.txn(20, account='card', description='Refund')
        all_data = analytics.analytics_bundle(self.conn)
        filtered = analytics.analytics_bundle(self.conn, exclude_outliers=True)
        self.assertEqual(all_data['stats']['total_spend'], 1030)
        self.assertEqual(filtered['stats']['total_spend'], 30)
        self.assertEqual(filtered['stats']['recorded_spend'], 1030)
        self.assertEqual(filtered['stats']['excluded_outlier_spend'], 1000)
        self.assertEqual(filtered['stats']['excluded_outlier_count'], 1)
        self.assertEqual(sum(sum(s['data']) for s in filtered['category_months']['series']), 30)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 7)

    def test_outlier_history_is_stable_across_date_selection(self):
        for i in range(5):
            self.txn(-10, day=f'2026-01-{1+i*3:02d}', merchant='Same shop')
        self.txn(-1000, day='2026-01-20', merchant='Same shop')
        result = analytics.analytics_bundle(self.conn, date_from='2026-01-20', date_to='2026-01-20', exclude_outliers=True)
        self.assertEqual(result['stats']['total_spend'], 0)
        self.assertEqual(result['stats']['recorded_spend'], 1000)
        self.assertEqual(result['monthly'][0]['excluded_outlier_spend'], 1000)
        self.assertTrue(result['monthly'][0]['partial_selection'])

    def test_complete_months_and_empty_gaps_have_explicit_coverage(self):
        self.conn.execute("INSERT INTO statements(account_id,source_file,period_start,period_end,reconciled) VALUES('bank','coverage','2026-01-01','2026-03-31',1)")
        self.txn(-30, day='2026-01-05')
        self.txn(-90, day='2026-03-05')
        self.txn(-1000, day='2026-02-05', account='other')
        result = analytics.analytics_bundle(self.conn, account='bank', date_from='2026-01-01', date_to='2026-03-31', today=date(2026, 4, 5))
        self.assertEqual(result['stats']['complete_months'], 3)
        self.assertEqual(result['stats']['typical_monthly_spend'], 30)
        self.assertEqual(result['monthly'][1]['spend'], 0)
        self.assertFalse(result['monthly'][1]['has_observations'])
        self.assertTrue(result['monthly'][1]['coverage_complete'])
        partial = analytics.analytics_bundle(self.conn, account='bank', date_from='2026-02-15', date_to='2026-03-31', today=date(2026, 4, 5))
        self.assertFalse(partial['monthly'][0]['coverage_complete'])
        self.assertEqual(partial['stats']['complete_months'], 1)
        self.assertEqual(partial['stats']['typical_monthly_spend'], 90)

    def test_monthly_category_drivers_sum_to_spending_change(self):
        self.txn(-100, day='2026-01-05')
        self.txn(20, day='2026-01-06', description='Refund')
        self.txn(-30, day='2026-01-05', category='Groceries')
        self.txn(-60, day='2026-02-05', category='Groceries')
        self.txn(-50, day='2026-02-05')
        self.txn(70, day='2026-02-06', description='Refund')
        result = analytics.analytics_bundle(self.conn)
        self.assertEqual(result['comparison']['change_amount'], -70)
        self.assertEqual(sum(r['change_amount'] for r in result['drivers']), -70)
        self.assertFalse(result['comparison']['comparable'])

    def test_friend_payments_are_not_category_size_outliers(self):
        for i in range(5):
            self.txn(-10, day=f'2026-01-{1+i*4:02d}', merchant=f'Shop {i}')
        person = self.txn(-500, merchant='Friend')
        self.conn.execute("UPDATE transactions SET merchant_kind='person' WHERE id=?", (person,))
        self.assertEqual(analytics.detect_anomalies(self.conn), [])
        self.assertEqual(analytics.analytics_bundle(self.conn)['stats']['total_spend'], 550)

    def test_invalid_date_ranges_are_rejected(self):
        with self.assertRaises(ValueError):
            analytics.analytics_bundle(self.conn, date_from='2026-02-30')
        with self.assertRaises(ValueError):
            analytics.analytics_bundle(self.conn, date_from='2026-03-01', date_to='2026-02-28')

    def test_final_calendar_month_is_finite_and_statement_end_does_not_overflow(self):
        self.conn.execute("INSERT INTO statements(account_id,source_file,period_start,period_end,reconciled) VALUES('bank','last-month','9999-12-01','9999-12-31',1)")
        result = analytics.analytics_bundle(self.conn, account='bank', date_from='9999-12-01', date_to='9999-12-31')
        self.assertEqual([row['m'] for row in result['monthly']], ['9999-12'])
        self.assertEqual(result['monthly'][0]['missing_accounts'], [])
        self.assertTrue(result['monthly'][0]['month_in_progress'])

    def test_uncategorized_payments_are_not_comparable_size_peers(self):
        for i in range(5):
            self.txn(-10, day=f'2026-01-{1+i*4:02d}', merchant=f'Unknown {i}', category='Uncategorized')
        self.txn(-1700, day='2026-02-01', merchant='Unclassified housing provider', category='Uncategorized')
        self.assertEqual(analytics.detect_anomalies(self.conn), [])
        self.assertEqual(analytics.analytics_bundle(self.conn, exclude_outliers=True)['stats']['total_spend'], 1750)

    def test_duplicate_candidates_are_not_automatically_removed_from_spending(self):
        self.txn(-10, day='2026-01-05')
        self.txn(-10, day='2026-01-06')
        self.assertEqual(analytics.detect_anomalies(self.conn)[0]['kind'], 'possible_duplicate')
        result = analytics.analytics_bundle(self.conn, exclude_outliers=True)
        self.assertEqual(result['stats']['total_spend'], 20)
        self.assertEqual(result['stats']['excluded_outlier_count'], 0)

    def test_stable_repeat_cost_is_protected_from_category_fallback(self):
        for i in range(8):
            self.txn(-10, day=f'2026-01-{1+i*3:02d}', merchant=f'Cheap service {i}', category='Subscriptions')
        for month in ('01', '02', '03'):
            self.txn(-200, day=f'2026-{month}-01', merchant='Premium service', category='Subscriptions')
        self.assertEqual(analytics.detect_anomalies(self.conn), [])

    def test_contextual_direction_prefix_uses_the_same_merchant_history(self):
        for month in ('01', '02', '03', '04', '05'):
            self.txn(-1000, day=f'2026-{month}-01', merchant='Housing provider', description='To Housing provider', category='Housing', code='BP')
        self.txn(-5000, day='2026-06-01', merchant='To Housing provider', category='Housing', code='BP')
        flagged = analytics.detect_anomalies(self.conn)
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]['comparison_basis'], 'merchant')
        self.assertEqual(flagged[0]['history_count'], 5)
        self.assertEqual(flagged[0]['baseline_amount'], 1000)

    def test_currency_exchange_principal_is_excluded_but_printed_fee_remains(self):
        row_id = self.txn(-84.42, merchant='Currency exchange', description='Exchanged to EUR',
                          category='Transfers & payments', meta={'currency_exchange': True,
                          'exchange_currency': 'EUR', 'exchange_fee_gbp': .84, 'exchange_principal_gbp': 83.58})
        self.conn.execute("UPDATE transactions SET spending_treatment='self_transfer' WHERE id=?", (row_id,))
        result = analytics.analytics_bundle(self.conn)
        self.assertEqual(result['stats']['total_spend'], .84)
        self.assertEqual(result['category_totals'], [{'category': 'Fees & interest', 'spend': .84}])
        self.assertEqual(result['exclusions'][0]['reason'], 'Currency conversions')
        self.assertEqual(result['exclusions'][0]['outflow'], 83.58)
        component = analytics.cashflow_rows(self.conn)[0]
        self.assertEqual((component['id'], component['original_amount'], component['amount']), (row_id, -84.42, -.84))
        self.assertEqual(self.conn.execute('SELECT amount FROM transactions WHERE id=?', (row_id,)).fetchone()[0], -84.42)
        self.txn(84.42, account='other', category='Income', code='FPI')
        self.assertEqual(analytics.detect_transfers(self.conn), [])

    def test_currency_exchange_fee_respects_explicit_exclusion(self):
        row_id = self.txn(-84.42, category='Transfers & payments', meta={'currency_exchange': True, 'exchange_fee_gbp': .84})
        self.conn.execute("UPDATE transactions SET spending_treatment='self_transfer',exclude_from_analysis=1 WHERE id=?", (row_id,))
        result = analytics.analytics_bundle(self.conn)
        self.assertEqual(result['stats']['total_spend'], 0)
        excluded = {r['reason']: r for r in result['exclusions']}
        self.assertEqual(excluded['Currency conversions']['outflow'], 83.58)
        self.assertEqual(excluded['User exclusions']['outflow'], .84)
        self.assertAlmostEqual(sum(r['outflow'] for r in excluded.values()), 84.42)

    def test_cost_basis_resets_after_complete_disposal(self):
        self.trade(10, -100, '2026-01-01')
        self.trade(-10, 150, '2026-02-01')
        self.trade(10, -200, '2026-03-01')
        position = analytics._holding_positions(self.conn)['GBTEST']
        self.assertEqual(position['cost_basis'], 200)
        self.assertEqual(position['avg_cost'], 20)
        self.assertEqual(position['realised'], 50)

    def test_partial_disposal_uses_chronological_average_and_same_day_time(self):
        # Insert out of order; original timestamps must establish trade order.
        self.trade(-5, 75, '2026-01-01', time='2026-01-01 12:00:00')
        self.trade(10, -100, '2026-01-01', time='2026-01-01 10:00:00')
        self.trade(5, -100, '2026-01-02')
        position = analytics._holding_positions(self.conn)['GBTEST']
        self.assertEqual(position['cost_basis'], 150)
        self.assertEqual(position['net_shares'], 10)
        self.assertEqual(position['realised'], 25)

    def test_unknown_native_cost_is_not_reported_as_zero(self):
        self.trade(10, 0, '2026-01-01', native_currency='USD', native_amount=-100, cost_basis_gbp=None)
        self.price('GBTEST', 20, '2026-01-05T10:00:00+00:00')
        totals = analytics.holdings_value(self.conn, now=datetime(2026, 1, 5, 10, tzinfo=timezone.utc))
        self.assertEqual(totals['market_value'], 200)
        self.assertIsNone(totals['cost_basis'])
        self.assertIsNone(totals['unrealised'])
        self.assertFalse(totals['cost_basis_complete'])
        self.assertTrue(totals['valuation_complete'])

    def test_missing_opening_lot_flags_incomplete_positions(self):
        self.trade(-10, 100, '2026-01-01')
        self.trade(20, -400, '2026-02-01')
        totals = analytics.holdings_value(self.conn)
        self.assertFalse(totals['position_complete'])
        self.assertFalse(totals['valuation_complete'])
        self.assertIsNone(totals['realised'])

    def test_partial_quotes_and_oldest_timestamp_are_explicit(self):
        for isin in ['A', 'B', 'C']:
            self.trade(10, -100, '2026-01-01', isin=isin)
        self.price('A', 15, '2026-01-05T10:00:00+00:00')
        self.price('B', 20, '2026-01-04T10:00:00+00:00')
        self.price('SOLD', 999, '2026-01-06T10:00:00+00:00')
        now = datetime(2026, 1, 5, 10, tzinfo=timezone.utc)
        totals = analytics.holdings_value(self.conn, now=now)
        self.assertEqual(totals['market_value'], 350)
        self.assertEqual(totals['n_unpriced'], 1)
        self.assertEqual(totals['n_stale'], 1)
        self.assertIsNone(totals['unrealised'])
        self.assertEqual(totals['asof'], '2026-01-04T10:00:00+00:00')
        self.assertFalse(totals['valuation_complete'])

    def test_failed_refresh_preserves_price_and_flags_staleness(self):
        self.trade(10, -100, '2026-01-01')
        self.price('GBTEST', 15, '2026-01-05T10:00:00+00:00')
        with patch('app.prices.batch_quote', return_value={}):
            result = analytics.valuation(self.conn, now=datetime(2026, 2, 1, tzinfo=timezone.utc))
        self.assertEqual(result['totals']['market_value'], 150)
        self.assertTrue(result['holdings'][0]['stale'])

    def test_multiple_investment_accounts_keep_separate_book_cost(self):
        self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('invest2','Other broker','investment','Other broker')")
        self.trade(10, -100, '2026-01-01')
        self.trade(10, -300, '2026-01-01', account='invest2')
        self.trade(-10, 150, '2026-01-02')
        position = analytics._holding_positions(self.conn)['GBTEST']
        self.assertEqual(position['cost_basis'], 300)
        self.assertEqual(position['realised'], 50)
        self.assertEqual(analytics.investments_bundle(self.conn)['stats']['cash_balance'], -250)

    def test_native_trade_summary_does_not_claim_zero_investment_cost(self):
        self.trade(10, 0, '2026-01-01', native_currency='USD', gbp_value_known=False)
        bundle = analytics.investments_bundle(self.conn)
        self.assertIsNone(bundle['stats']['invested'])
        self.assertFalse(bundle['stats']['cash_history_complete'])
        self.assertIsNone(bundle['holdings'][0]['net'])
        self.assertEqual(bundle['stats']['n_holdings'], 1)


    def test_foreign_cash_is_disclosed_separately(self):
        self.txn(-80, account='invest', category='Investments',
                 meta={'cash_movements': {'GBP': -80, 'USD': 100}})
        self.trade(1, 0, '2026-01-05', native_currency='USD', cash_movements={'USD': -60})
        result = analytics.holdings_value(self.conn)
        self.assertEqual(result['foreign_cash'], [{'currency': 'USD', 'amount': 40, 'value_gbp': None}])
        self.assertEqual(result['foreign_cash_unvalued'], 1)


class PriceTests(unittest.TestCase):
    def setUp(self):
        prices._fx_cache.clear()

    def test_unknown_currency_cannot_be_assumed_to_be_gbp(self):
        metadata = {'chart': {'result': [{'meta': {'symbol': 'CUKX.L', 'regularMarketPrice': 100,
                    'regularMarketTime': 1767607200}}]}}
        with patch('app.prices._get_json', return_value=metadata):
            self.assertIsNone(prices.quote('IE00B53HP851', ticker='CUKX'))
        self.assertEqual(prices._fx_to_gbp('GBp'), 0.01)
        self.assertEqual(prices._fx_to_gbp(' GBP '), 1)

    def test_fx_cache_expires(self):
        fx = {'symbol': 'GBPUSD=X', 'currency': 'USD', 'price': 1.25,
              'asof': '2026-01-05T10:00:00+00:00', 'exchange': 'CCY',
              'instrument_type': 'CURRENCY', 'source_url': 'https://finance.yahoo.com/quote/GBPUSD%3DX/'}
        with patch('app.prices.time.monotonic', return_value=1000), patch('app.prices.fetch_market_quote', return_value=fx) as fetch:
            self.assertAlmostEqual(prices._fx_to_gbp('USD'), .8)
            self.assertAlmostEqual(prices._fx_to_gbp('USD'), .8)
            self.assertEqual(fetch.call_count, 1)
        with patch('app.prices.time.monotonic', return_value=2000), patch('app.prices.fetch_market_quote', return_value={**fx, 'price': 2}):
            self.assertEqual(prices._fx_to_gbp('USD'), .5)

    def test_nonfinite_price_is_rejected(self):
        metadata = {'chart': {'result': [{'meta': {'symbol': 'CUKX.L', 'currency': 'GBp',
                    'regularMarketPrice': float('nan'), 'regularMarketTime': 1767607200}}]}}
        with patch('app.prices._get_json', return_value=metadata):
            self.assertIsNone(prices.quote('IE00B53HP851', ticker='CUKX'))


if __name__ == '__main__':
    unittest.main()

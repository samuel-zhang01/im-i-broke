"""Fixed-basket market estimates must never become fabricated account history."""
from copy import deepcopy
from datetime import datetime, timezone
import sqlite3
import unittest

from app import market_valuation as valuation

NOW = datetime(2026, 9, 18, 15, 40, tzinfo=timezone.utc)


class MarketValuationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.execute('CREATE TABLE reference_fx(currency TEXT,date TEXT,rate_to_gbp REAL,observed_date TEXT)')
        self.conn.executemany('INSERT INTO reference_fx VALUES(?,?,?,?)', [
            ('USD','2026-09-17',.75,'2026-09-17'), ('USD','2026-09-18',.8,'2026-09-18')])
        self.position = {'id':'p', 'instrument_id':'test', 'raw_symbol':'TEST', 'exchange':'XNYS',
                         'listing_currency':'USD', 'quantity':2, 'asset_type':'stock'}
        self.account = {'id':'a', 'name':'Synthetic', 'total_value':9999, 'currency':'GBP',
            'holdings_synced_at':'2026-09-18T12:00:00Z', 'is_paper':False,
            'positions': {'status':'ok','data':[self.position]},
            'balances': {'status':'ok','data':[{'currency':'GBP','cash':20,'buying_power':5000}]}}
        self.cached = {'accounts':[self.account]}
        self.markets = {'test': {'symbol':'TEST','currency':'USD','exchange':'NYQ','status':'ok',
            'points':[{'timestamp':'2026-09-17T15:00:00Z','close':100},
                      {'timestamp':'2026-09-18T14:00:00Z','close':110},
                      {'timestamp':'2026-09-18T15:00:00Z','close':120}]}}

    def data(self, **kwargs):
        return valuation.bundle(self.conn, now=NOW, cached=self.cached, markets=self.markets, **kwargs)

    def test_hourly_value_is_current_basket_plus_cash_once_with_dated_fx(self):
        before = deepcopy(self.cached)
        result = self.data()
        self.assertEqual(len(result['points']),26)
        self.assertEqual(result['points'][0]['value'],170)  # 2 * 100 * .75 + 20
        self.assertEqual(result['summary']['value'],212)   # 2 * 120 * .8 + 20
        self.assertEqual(result['summary']['change'],42)
        self.assertEqual(result['basis'],'current_holdings')
        self.assertEqual(result['market_as_of'],'2026-09-18T15:00:00Z')
        self.assertEqual(self.cached,before)  # Broker NAV is neither added nor overwritten.

    def test_future_close_cannot_appear_in_an_earlier_hour(self):
        self.markets['test']['points'].append({'timestamp':'2026-09-18T16:00:00Z','close':10000})
        result = self.data()
        points={p['timestamp']:p for p in result['points']}
        self.assertEqual(points['2026-09-18T13:00:00Z']['value'],180)
        self.assertEqual(points['2026-09-18T14:00:00Z']['value'],196)
        self.assertEqual(result['summary']['value'],212)
        self.assertEqual(points['2026-09-18T13:00:00Z']['oldest_price_at'],'2026-09-17T15:00:00Z')

    def test_daily_interval_uses_last_available_utc_hour_without_adding_samples(self):
        result=self.data(interval='1d')
        self.assertEqual([p['timestamp'] for p in result['points']], [
            '2026-09-17T15:40:00Z','2026-09-17T23:00:00Z','2026-09-18T15:40:00Z'])
        self.assertEqual(result['summary']['value'],212)

    def test_daily_sampling_cannot_change_period_movement_or_comparison_start(self):
        # A material move after the selected start used to disappear when the
        # daily sampler replaced that boundary with the first day's last point.
        self.markets['test']['points'].insert(1, {'timestamp':'2026-09-17T16:00:00Z','close':200})
        hourly, daily = self.data(interval='1h'), self.data(interval='1d')
        self.assertEqual(hourly['summary']['change'],42)
        self.assertEqual(hourly['summary'],daily['summary'])
        self.assertEqual(hourly['points'][0],daily['points'][0])
        self.assertEqual(hourly['points'][-1],daily['points'][-1])

    def test_daily_sampling_preserves_midday_gap_and_withholds_period_change(self):
        self.markets['test']['points'] = [
            {'timestamp':'2026-09-13T18:00:00Z','close':100},
            {'timestamp':'2026-09-17T22:00:00Z','close':110},
            {'timestamp':'2026-09-18T15:00:00Z','close':120},
        ]
        hourly, daily = self.data(interval='1h'), self.data(interval='1d')
        self.assertEqual(hourly['summary'],daily['summary'])
        self.assertIsNone(daily['summary']['change'])
        self.assertEqual(daily['status'],'partial')
        self.assertTrue(any(point['value'] is None for point in daily['points']))
        self.assertEqual(hourly['points'][0],daily['points'][0])

    def test_unknown_cash_is_not_zero_and_unknown_account_is_explicitly_partial(self):
        unknown=deepcopy(self.account);unknown.update(id='unknown',holdings_synced_at=None)
        unknown['positions']={'status':'unsupported','data':[]};unknown['balances']={'status':'unsupported','data':[]}
        self.cached['accounts'].append(unknown)
        result=self.data()
        self.assertEqual(result['summary']['value'],212)
        self.assertEqual(result['summary']['known_accounts'],1)
        self.assertEqual(result['summary']['selected_accounts'],2)
        self.assertTrue(result['summary']['partial'])
        self.account['balances']['data'][0]['cash']=None
        self.assertIsNone(self.data()['summary']['value'])

    def test_missing_fx_or_mismatched_listing_never_makes_a_partial_total(self):
        self.conn.execute('DELETE FROM reference_fx')
        self.assertIsNone(self.data()['summary']['value'])
        self.markets['test']['currency']='GBP'
        self.assertEqual(self.data()['status'],'unavailable')

    def test_stale_week_old_quotes_are_not_carried_indefinitely(self):
        self.markets['test']['points']=[{'timestamp':'2026-09-10T15:00:00Z','close':100}]
        self.assertEqual(self.data()['points'],[])

    def test_duplicate_positions_and_unavailable_breakdowns_do_not_double_count(self):
        self.account['positions']['data'].append(deepcopy(self.position))
        self.assertIsNone(self.data()['summary']['value'])
        self.account['positions']['data']=[]
        self.account['balances']['data'][0]['cash']=0
        self.assertIsNone(self.data()['summary']['value'])

    def test_filtered_accounts_and_validation(self):
        self.assertIsNone(self.data(account_ids=['__none__'])['summary']['value'])
        self.assertEqual(self.data(account_ids=['a','a'])['summary']['selected_accounts'],1)
        for changes in ({'account_ids':['missing']},{'range_name':'1h'},{'interval':'5m'}):
            with self.assertRaises(ValueError): self.data(**changes)

    def test_retained_fetch_failure_is_labelled_partial(self):
        self.markets['test']['status']='stale';self.markets['test']['error']='Refresh failed'
        result=self.data()
        self.assertEqual(result['summary']['value'],212)
        self.assertEqual(result['status'],'partial')

    def test_latest_completed_half_hour_bar_is_in_current_endpoint(self):
        self.markets['test']['points'].append({'timestamp':'2026-09-18T15:30:00Z','close':130})
        result=self.data()
        self.assertEqual(result['points'][-1]['timestamp'],'2026-09-18T15:40:00Z')
        self.assertEqual(result['summary']['value'],228)
        self.assertEqual(result['market_as_of'],'2026-09-18T15:30:00Z')

    def test_split_after_saved_quantity_excludes_stale_share_basis(self):
        self.markets['test']['splits']=[{'timestamp':'2026-09-18T13:30:00Z','numerator':2,'denominator':1}]
        result=self.data()
        self.assertIsNone(result['summary']['value'])
        self.assertTrue(any('split occurred' in warning for warning in result['warnings']))
        self.account['holdings_synced_at']='2026-09-18T14:00:00Z'
        self.assertEqual(self.data()['summary']['value'],212)

    def test_zero_foreign_cash_does_not_require_fx_but_duplicate_cash_is_ambiguous(self):
        self.account['balances']['data'].append({'currency':'SGD','cash':0})
        self.assertEqual(self.data()['summary']['value'],212)
        self.account['balances']['data'].append({'currency':'GBP','cash':20})
        self.assertIsNone(self.data()['summary']['value'])

    def test_cash_only_breakdown_must_reconcile_saved_nav(self):
        self.account['positions']={'status':'empty','data':[]}
        self.assertIsNone(self.data()['summary']['value'])
        self.account['total_value']=20
        self.assertEqual(self.data()['summary']['value'],20)
        self.account['initial_sync_complete']=False
        self.assertIsNone(self.data()['summary']['value'])

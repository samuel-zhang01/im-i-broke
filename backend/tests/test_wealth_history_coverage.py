"""Explain historical account coverage without changing any source balances."""
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app import investing_analytics, snaptrade_models, snaptrade_store, source_reconciliation, wealth


TODAY = date(2026, 9, 18)


def account(identifier, current, values=()):
    return {'id': identifier, 'name': 'Synthetic ' + identifier, 'institution': 'Test broker',
            'currency': 'GBP', 'total_value': current, 'is_paper': False,
            'holdings_synced_at': TODAY.isoformat(),
            'total_value_as_of': TODAY.isoformat(),
            'history': {'data': [{'date': when, 'total_value': value, 'currency': 'GBP'} for when, value in values]}}


class WealthHistoryCoverageTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE accounts(id TEXT PRIMARY KEY, bank TEXT, account_type TEXT, display_name TEXT);
            CREATE TABLE statements(id INTEGER PRIMARY KEY, account_id TEXT, period_start TEXT, period_end TEXT,
                opening_balance REAL, closing_balance REAL, reconciled INTEGER, meta TEXT);
            CREATE TABLE reference_fx(date TEXT,currency TEXT,rate_to_gbp REAL,source TEXT,observed_date TEXT,fetched_at TEXT,source_url TEXT,PRIMARY KEY(date,currency));
        ''')
        self.cached = {'accounts': [], 'connections': {'data': []}, 'retrieved_at': TODAY.isoformat(), 'cache': {}}
        self.observations = []
        for name, side_effect in [
            ('load_portfolio', lambda **_: self.cached),
            ('list_account_observations', lambda **_: self.observations),
            ('list_account_estimates', lambda **_: []),
        ]:
            mocked = patch.object(wealth.snaptrade_store, name, side_effect=side_effect)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.replay = {'sources': {}}
        mocked = patch('app.investment_history.reconstruct', side_effect=lambda *args, **kwargs: self.replay)
        mocked.start()
        self.addCleanup(mocked.stop)

    def bundle(self):
        return wealth.bundle(self.conn, today=TODAY, range_name='1m')

    def test_both_funded_accounts_remain_in_history_when_zero_and_unknown_accounts_are_excluded(self):
        days = ['2026-09-14', '2026-09-15', '2026-09-16']
        self.cached['accounts'] = [
            account('reconstructed', 1200, zip(days, [500, 550, 560])),
            account('provider', 1000, zip(days, [100, 110, 110])),
            account('zero-a', 0, zip(days, [0, 0, 0])),
            account('zero-b', 0, zip(days, [0, 0, 0])),
            account('unknown', None, zip(days, [0, 0, 0])),
        ]
        self.replay['sources']['connected:reconstructed'] = {
            'observations': [
                {'date': when, 'value': value, 'currency': 'GBP', 'source': 'reconstructed',
                 'estimated': True, 'reconciled': True, 'valuation_basis': 'daily_holdings'}
                for when, value in zip(days, [1000, 1100, 1150])
            ], 'coverage': {'valued_days': 3}, 'issues': [],
        }
        original = deepcopy(self.cached)
        data = self.bundle()
        history = data['history']['cohort']
        rows = {row['account_id']: row for row in history['account_coverage']}
        self.assertEqual(history['account_ids'], ['connected:provider', 'connected:reconstructed'])
        self.assertEqual(history['end_value'], 2200)
        self.assertEqual(history['end_date'], '2026-09-18')
        self.assertEqual(history['current_value'], 2200)
        self.assertEqual(data['summary']['net_worth'], 2200)
        self.assertTrue(history['current_shown_separately'])
        self.assertEqual(history['continuity'], {
            'last_continuous_date': '2026-09-16',
            'recorded_start_date': '2026-09-18', 'recorded_end_date': '2026-09-18', 'recorded_days': 1,
            'gaps': [{'start_date': '2026-09-17', 'end_date': '2026-09-17', 'days': 1,
                      'reason': 'missing_dated_account_values', 'account_ids': ['connected:reconstructed']}],
        })
        points = {point['date']: point for point in history['points']}
        self.assertEqual(points['2026-09-16']['net_worth'], 1260)
        self.assertIsNone(points['2026-09-17']['net_worth'])
        self.assertNotEqual(points['2026-09-16']['valuation_key'], points['2026-09-18']['valuation_key'])
        self.assertFalse(history['change_comparable'])
        self.assertIsNone(history['change'])
        self.assertEqual(rows['connected:reconstructed']['history_end_value'], 1200)
        self.assertEqual(rows['connected:reconstructed']['history_valuation_basis'], 'recorded_balance')
        self.assertEqual(rows['connected:reconstructed']['provider_conflict_dates'], 3)
        self.assertEqual(rows['connected:reconstructed']['reconstructed_through'], '2026-09-16')
        self.assertEqual(rows['connected:reconstructed']['recorded_days'], 1)
        self.assertEqual(rows['connected:provider']['history_end_value'], 1000)
        self.assertEqual(rows['connected:provider']['history_valuation_basis'], 'recorded_balance')
        self.assertEqual(rows['connected:provider']['latest_observation_date'], '2026-09-18')
        self.assertEqual(sum(row['history_end_value'] for row in rows.values() if row['included']), history['end_value'])
        for identifier in ('zero-a', 'zero-b'):
            row = rows['connected:' + identifier]
            self.assertEqual(row['reason'], 'zero_balance')
            self.assertEqual(row['current_value'], 0)
            self.assertIsNone(row['history_end_date'])
            self.assertIsNone(row['history_end_value'])
            self.assertIsNone(row['history_valuation_basis'])
        self.assertEqual(rows['connected:unknown']['reason'], 'current_value_unknown')
        self.assertIsNone(rows['connected:unknown']['current_value'])
        self.assertEqual(history['coverage_summary'], {
            'included_accounts': 2, 'included_nonzero_accounts': 2, 'included_zero_accounts': 0, 'included_unknown_accounts': 0,
            'excluded_nonzero_accounts': 0, 'excluded_zero_accounts': 2, 'excluded_unknown_accounts': 1,
        })
        self.assertEqual(self.cached, original)

    def test_excluded_funded_accounts_distinguish_missing_history_from_shorter_shared_coverage(self):
        self.cached['accounts'] = [
            account('main', 100000, [('2026-09-12', 100000), ('2026-09-13', 100100), ('2026-09-14', 100200)]),
            account('small', 100, [('2026-09-14', 90), ('2026-09-15', 95)]),
            account('new', 50),
        ]
        data = self.bundle()
        rows = {row['account_id']: row for row in data['history']['cohort']['account_coverage']}
        self.assertEqual(rows['connected:main']['reason'], 'included')
        self.assertEqual(rows['connected:small']['reason'], 'shorter_shared_history')
        self.assertEqual(rows['connected:new']['reason'], 'insufficient_history')
        self.assertEqual(data['history']['cohort']['coverage_summary']['excluded_nonzero_accounts'], 2)
        self.assertEqual(data['summary']['net_worth'], 100150)

    def test_full_net_history_backfills_only_supported_complete_dates(self):
        self.cached['accounts'] = [
            account('main', 1000, [('2026-09-14', 900), ('2026-09-15', 950)]),
            account('later', 500, [('2026-09-15', 450), ('2026-09-16', 475)]),
        ]
        data = self.bundle()
        history = data['history']
        self.assertEqual(history['mode'], 'net_assets')
        self.assertEqual(history['account_ids'], ['connected:main', 'connected:later'])
        self.assertEqual(history['net_start_date'], '2026-09-15')
        self.assertEqual(history['net_points'][0]['net_worth'], 1400)
        self.assertEqual(history['net_points'][-1]['net_worth'], data['summary']['net_worth'])
        self.assertTrue(all(point['known_accounts'] == 2 and point['complete']
                            for point in history['net_points']))
        self.assertTrue(all(point['date'] >= history['net_start_date']
                            for point in history['net_points']))
        self.assertEqual(history['points'], history['net_points'])

    def test_investment_history_extends_before_bank_coverage_without_relabeling_net_assets(self):
        self.cached['accounts'] = [account('broker', 1000, [
            ('2026-09-14', 900), ('2026-09-15', 925),
            ('2026-09-16', 950), ('2026-09-17', 975),
        ])]
        self.conn.execute("INSERT INTO accounts VALUES('cash','Synthetic bank','current','Cash')")
        self.conn.execute("""INSERT INTO statements(account_id,period_end,closing_balance,reconciled)
            VALUES('cash','2026-09-17',100,1)""")
        data = self.bundle()
        history = data['history']
        investment = history['investment_history']
        self.assertEqual(history['start_date'], '2026-09-17')
        self.assertEqual(investment['account_ids'], ['connected:broker'])
        self.assertEqual(investment['start_date'], '2026-09-14')
        self.assertEqual(investment['points'][0]['net_worth'], 900)
        self.assertEqual(investment['points'][-1]['net_worth'], 1000)
        self.assertEqual(history['points'][-1]['net_worth'], data['summary']['net_worth'])
        self.assertTrue(all(point['complete'] for point in investment['points']))
        self.assertTrue(all(point['date'] >= history['start_date'] for point in history['points']))

    def test_investment_history_requires_every_selected_investment_account(self):
        self.cached['accounts'] = [
            account('early', 1000, [('2026-09-14', 900), ('2026-09-15', 925)]),
            account('later', 500, [('2026-09-16', 475)]),
        ]
        self.conn.execute("INSERT INTO accounts VALUES('cash','Synthetic bank','current','Cash')")
        self.conn.execute("""INSERT INTO statements(account_id,period_end,closing_balance,reconciled)
            VALUES('cash','2026-09-17',100,1)""")
        history = self.bundle()['history']
        investment = history['investment_history']
        self.assertEqual(investment['start_date'], '2026-09-16')
        self.assertEqual(investment['points'][0]['net_worth'], 1400)
        self.assertEqual(investment['account_ids'], ['connected:early', 'connected:later'])
        self.assertEqual(history['start_date'], '2026-09-17')

    def test_full_net_history_preserves_older_supported_dates_across_missing_days(self):
        self.cached['accounts'] = [account('recorded', 1030)]
        self.observations = [
            {'account_id': 'recorded', 'name': 'Synthetic recorded', 'institution': 'Test broker',
             'observed_at': when, 'retrieved_at': when, 'total_value': value, 'currency': 'GBP'}
            for when, value in [('2026-09-14', 1000), ('2026-09-16', 1020),
                                ('2026-09-18', 1030)]
        ]
        data = self.bundle()
        history = data['history']
        points = {point['date']: point for point in history['points']}
        self.assertEqual(history['mode'], 'net_assets')
        self.assertEqual(history['start_date'], '2026-09-14')
        self.assertEqual(points['2026-09-14']['net_worth'], 1000)
        self.assertIsNone(points['2026-09-15']['net_worth'])
        self.assertEqual(points['2026-09-16']['net_worth'], 1020)
        self.assertIsNone(points['2026-09-17']['net_worth'])
        self.assertEqual(points['2026-09-18']['net_worth'], data['summary']['net_worth'])
        self.assertFalse(history['change_comparable'])
        self.assertIsNone(history['change'])

    def test_empty_history_and_unknown_current_balance_remain_unknown(self):
        self.cached['accounts'] = [account('new', 100), account('unknown', None)]
        history = self.bundle()['history']['cohort']
        self.assertEqual(history['points'], [])
        self.assertEqual(history['coverage_summary']['included_accounts'], 0)
        self.assertEqual(history['coverage_summary']['excluded_nonzero_accounts'], 1)
        self.assertEqual(history['coverage_summary']['excluded_unknown_accounts'], 1)
        self.assertTrue(all(row['history_end_value'] is None for row in history['account_coverage']))
        self.assertTrue(all(row['history_end_date'] is None for row in history['account_coverage']))

    def test_provider_discontinuity_and_changed_valuation_basis_never_report_summary_growth(self):
        self.cached['accounts'] = [account('provider', 117000, [
            ('2026-09-14', 14800), ('2026-09-16', 14820),
        ])]
        data = self.bundle()
        self.assertEqual(data['summary']['net_worth'], 117000)
        self.assertEqual(data['history']['cohort']['end_value'], 117000)
        self.assertEqual(data['history']['cohort']['end_date'], TODAY.isoformat())
        self.assertIsNone(data['history']['cohort']['change'])
        self.assertFalse(data['history']['cohort']['change_comparable'])
        self.assertTrue(data['history']['cohort']['points'][-1]['source_break'])
        self.assertEqual(data['history']['cohort']['account_coverage'][0]['source_discontinuities'], 1)
        self.assertFalse(data['summary']['change_comparable'])
        self.assertIsNone(data['summary']['change'])
        self.assertIsNone(data['summary']['change_pct'])
        self.assertEqual(data['points'][-1]['net_worth'], 117000, 'Reported current observation stays intact')

    def test_even_a_small_provider_to_recorded_transition_is_not_a_comparable_summary(self):
        self.cached['accounts'] = [account('provider', 1030, [
            ('2026-09-14', 1000), ('2026-09-16', 1020),
        ])]
        data = self.bundle()
        self.assertEqual(data['history']['cohort']['account_coverage'][0]['source_discontinuities'], 0)
        self.assertFalse(data['summary']['change_comparable'])
        self.assertIsNone(data['summary']['change'])

    def test_consistent_recorded_balances_still_report_comparable_value_changes(self):
        self.cached['accounts'] = [account('recorded', 1030)]
        self.observations = [
            {'account_id': 'recorded', 'name': 'Synthetic recorded', 'institution': 'Test broker',
             'observed_at': when, 'retrieved_at': when, 'total_value': value, 'currency': 'GBP'}
            for when, value in [('2026-09-14', 1000), ('2026-09-15', 1005),
                                ('2026-09-16', 1020), ('2026-09-17', 1025)]
        ]
        data = self.bundle()
        self.assertTrue(data['summary']['change_comparable'])
        self.assertEqual(data['summary']['change'], 30)
        self.assertEqual(data['summary']['change_pct'], 3)
        self.assertTrue(data['history']['cohort']['change_comparable'])
        self.assertEqual(data['history']['cohort']['change'], 30)

    def test_provider_estimate_cannot_fill_between_recorded_days(self):
        self.cached['accounts'] = [account('recorded', 1030, [('2026-09-15', 1010)])]
        self.observations = [
            {'account_id': 'recorded', 'name': 'Synthetic recorded', 'institution': 'Test broker',
             'observed_at': when, 'retrieved_at': when, 'total_value': value, 'currency': 'GBP'}
            for when, value in [('2026-09-14', 1000), ('2026-09-16', 1020)]
        ]
        data = self.bundle()
        points = data['history']['cohort']['points']
        self.assertEqual(points[0]['valuation_key'], points[-1]['valuation_key'])
        self.assertNotEqual(points[0]['valuation_key'], points[1]['valuation_key'])
        self.assertIsNone(points[1]['net_worth'])
        self.assertEqual(points[1]['date'], '2026-09-15')
        for summary in (data['history']['cohort'], data['summary']):
            self.assertFalse(summary['change_comparable'])
            self.assertIsNone(summary['change'])
            self.assertIsNone(summary['change_pct'])

    def test_same_basis_after_a_long_gap_retains_today_without_a_period_comparison(self):
        self.cached['accounts'] = [account('recorded', 1030)]
        self.observations = [
            {'account_id': 'recorded', 'name': 'Synthetic recorded', 'institution': 'Test broker',
             'observed_at': when, 'retrieved_at': when, 'total_value': value, 'currency': 'GBP'}
            for when, value in [('2026-09-01', 1000), ('2026-09-02', 1020)]
        ]
        data = self.bundle()
        points = data['history']['cohort']['points']
        self.assertEqual(points[-1]['date'], TODAY.isoformat())
        self.assertEqual(points[-1]['net_worth'], data['summary']['net_worth'])
        self.assertEqual(points[0]['valuation_key'], points[-1]['valuation_key'])
        gaps = [point for point in points if not point['complete']]
        self.assertEqual([point['date'] for point in gaps], [f'2026-09-{day:02}' for day in range(3, 18)])
        self.assertTrue(all(point['net_worth'] is None for point in gaps))
        for summary in (data['history']['cohort'], data['summary']):
            self.assertFalse(summary['change_comparable'])
            self.assertIsNone(summary['change'])
            self.assertIsNone(summary['change_pct'])

    def test_current_value_uses_the_balance_clock_instead_of_newer_positions(self):
        self.cached['accounts'] = [account('recorded', 1030, [('2026-09-14', 1000)])]
        self.cached['accounts'][0].update(total_value_as_of='2026-09-16T09:20:00Z',
                                          holdings_synced_at='2026-09-18T16:30:00Z')
        data = self.bundle()
        row = data['accounts'][0]
        self.assertEqual(row['as_of'], '2026-09-16T09:20:00Z')
        self.assertTrue(row['source_time_known'])
        self.assertEqual(data['history']['cohort']['end_date'], '2026-09-16')
        self.assertEqual(data['history']['cohort']['continuity']['recorded_start_date'], '2026-09-16')
        self.assertEqual(data['history']['cohort']['account_coverage'][0]['current_value_as_of'], row['as_of'])
        self.assertFalse(any(point['date'] == '2026-09-18' for point in data['history']['cohort']['points']))

    def test_newer_provider_estimate_cannot_enter_history_after_live_collection_starts(self):
        self.cached['accounts'] = [account('recorded', 1030, [
            ('2026-09-14', 1000), ('2026-09-18', 9000),
        ])]
        self.cached['accounts'][0].update(total_value_as_of='2026-09-16T09:20:00Z',
                                          holdings_synced_at='2026-09-18T16:30:00Z')
        data = self.bundle()
        self.assertEqual(data['summary']['net_worth'], 1030)
        self.assertEqual(data['accounts'][0]['value'], 1030)
        self.assertEqual(data['accounts'][0]['as_of'], '2026-09-16T09:20:00Z')
        self.assertEqual(data['points'][-1]['net_worth'], 1030)
        self.assertEqual(data['history']['cohort']['points'][-1]['net_worth'], 1030)
        self.assertEqual(data['history']['cohort']['points'][-1]['date'], '2026-09-16')
        self.assertEqual(data['history']['cohort']['points'][-1]['valuation_basis'], 'recorded_balance')
        self.assertEqual(self.cached['accounts'][0]['history']['data'][-1]['total_value'], 9000,
                         'Provider evidence remains unchanged outside canonical history')

    def test_legacy_cache_uses_retrieval_time_without_claiming_the_positions_clock(self):
        self.cached['accounts'] = [account('recorded', 1030)]
        self.cached['retrieved_at'] = '2026-09-18T16:45:00Z'
        self.cached['accounts'][0].update(total_value_as_of=None, holdings_synced_at='2026-09-18T09:20:00Z')
        data = self.bundle()
        row = data['accounts'][0]
        self.assertEqual(row['as_of'], self.cached['retrieved_at'])
        self.assertFalse(row['source_time_known'])
        self.assertFalse(data['history']['cohort']['account_coverage'][0]['source_time_known'])

    def test_latest_same_day_balance_wins_even_when_older_data_is_retrieved_later(self):
        self.cached['accounts'] = [account('recorded', 900)]
        self.cached['accounts'][0]['total_value_as_of'] = '2026-09-18T09:00:00Z'
        self.cached['retrieved_at'] = '2026-09-18T17:00:00Z'
        self.observations = [
            {'account_id': 'recorded', 'name': 'Synthetic recorded', 'institution': 'Test broker',
             'observed_at': when, 'retrieved_at': '2026-09-18T16:00:00Z', 'total_value': value,
             'currency': 'GBP', 'source_time_known': True}
            for when, value in [('2026-09-18T15:00:00Z', 1500), ('2026-09-18T10:00:00Z', 1000)]
        ]
        data = self.bundle()
        self.assertEqual(data['summary']['net_worth'], 1500)
        self.assertEqual(data['accounts'][0]['as_of'], '2026-09-18T15:00:00Z')
        self.assertEqual(data['history']['cohort']['account_coverage'][0]['recorded_days'], 1)

    def test_fresh_balance_clock_supersedes_legacy_same_day_receipt_without_rewriting_it(self):
        self.cached['accounts'] = [account('recorded', 1100)]
        self.cached['accounts'][0]['total_value_as_of'] = '2026-09-18T09:18:00Z'
        self.cached['retrieved_at'] = '2026-09-18T17:00:00Z'
        self.observations = [
            {'account_id': 'recorded', 'name': 'Synthetic recorded', 'institution': 'Test broker',
             'observed_at': '2026-09-18T16:41:00Z', 'retrieved_at': '2026-09-18T16:41:00Z',
             'total_value': 1030, 'currency': 'GBP', 'source_time_known': False},
        ]
        original = deepcopy(self.observations)
        data = self.bundle()
        self.assertEqual(data['summary']['net_worth'], 1100)
        self.assertEqual(data['accounts'][0]['as_of'], '2026-09-18T09:18:00Z')
        self.assertTrue(data['accounts'][0]['source_time_known'])
        self.assertEqual(self.observations, original, 'Legacy evidence stays intact')

    def test_latest_received_total_without_source_clock_remains_visible_and_unknown(self):
        self.cached['accounts'] = [account('recorded', 1100)]
        self.cached['accounts'][0]['total_value_as_of'] = None
        self.cached['retrieved_at'] = '2026-09-18T17:00:00Z'
        self.observations = [
            {'account_id': 'recorded', 'name': 'Synthetic recorded', 'institution': 'Test broker',
             'observed_at': '2026-09-18T09:18:00Z', 'retrieved_at': '2026-09-18T16:41:00Z',
             'total_value': 1030, 'currency': 'GBP', 'source_time_known': True},
        ]
        data = self.bundle()
        self.assertEqual(data['summary']['net_worth'], 1100)
        self.assertEqual(data['accounts'][0]['as_of'], '2026-09-18T17:00:00Z')
        self.assertFalse(data['accounts'][0]['source_time_known'])

    def test_fresh_receipt_of_a_prior_day_balance_is_current_even_after_unknown_clock_snapshot(self):
        self.cached['accounts'] = [account('recorded', 1100)]
        self.cached['accounts'][0]['total_value_as_of'] = '2026-09-17T09:18:00Z'
        self.cached['retrieved_at'] = '2026-09-18T17:00:00Z'
        self.observations = [
            {'account_id': 'recorded', 'name': 'Synthetic recorded', 'institution': 'Test broker',
             'observed_at': '2026-09-18T16:41:00Z', 'retrieved_at': '2026-09-18T16:41:00Z',
             'total_value': 1030, 'currency': 'GBP', 'source_time_known': False},
        ]
        data = self.bundle()
        self.assertEqual(data['summary']['net_worth'], 1100)
        self.assertEqual(data['accounts'][0]['as_of'], '2026-09-17T09:18:00Z')
        self.assertTrue(data['accounts'][0]['source_time_known'])

    def test_balance_dates_and_latest_observation_order_use_the_same_utc_day(self):
        self.cached['accounts'] = [account('recorded', 900)]
        self.cached['accounts'][0]['total_value_as_of'] = '2026-09-18T09:00:00Z'
        self.observations = [
            {'account_id': 'recorded', 'name': 'Synthetic recorded', 'institution': 'Test broker',
             'observed_at': when, 'retrieved_at': TODAY.isoformat(), 'total_value': value,
             'currency': 'GBP', 'source_time_known': True}
            for when, value in [('2026-09-19T01:00:00+08:00', 1500), ('2026-09-18T16:00:00Z', 1000)]
        ]
        data = self.bundle()
        self.assertEqual(data['summary']['net_worth'], 1500)
        self.assertEqual(data['accounts'][0]['as_of'], '2026-09-19T01:00:00+08:00')
        self.assertEqual(data['history']['cohort']['account_coverage'][0]['recorded_start_date'], '2026-09-18')
        self.assertEqual(data['history']['cohort']['account_coverage'][0]['recorded_days'], 1)

    def test_next_days_recorded_value_extends_the_new_segment_without_filling_old_gaps(self):
        self.cached['accounts'] = [account('recorded', 1200)]
        self.replay['sources']['connected:recorded'] = {
            'observations': [{'date': when, 'value': value, 'currency': 'GBP', 'source': 'reconstructed',
                              'valuation_basis': 'daily_holdings', 'estimated': True, 'reconciled': True}
                             for when, value in [('2026-09-14', 1000), ('2026-09-15', 1100)]],
            'coverage': {}, 'issues': [],
        }
        first = self.bundle()
        self.assertEqual(first['history']['cohort']['continuity']['last_continuous_date'], '2026-09-15')
        self.assertEqual(first['history']['cohort']['continuity']['recorded_days'], 1)
        self.observations = [{'account_id': 'recorded', 'name': 'Synthetic recorded', 'institution': 'Test broker',
                              'observed_at': '2026-09-18T09:00:00Z', 'retrieved_at': saved,
                              'total_value': 1200, 'currency': 'GBP', 'source_time_known': True}
                             for saved in ['2026-09-18T10:00:00Z', '2026-09-18T16:00:00Z']]
        self.cached['accounts'][0].update(total_value=1210, total_value_as_of='2026-09-19T09:00:00Z',
                                          holdings_synced_at='2026-09-19T16:00:00Z')
        self.cached['retrieved_at'] = '2026-09-19T16:10:00Z'
        next_day = wealth.bundle(self.conn, today=date(2026, 9, 19), range_name='1m')
        history = next_day['history']['cohort']
        points = {point['date']: point for point in history['points']}
        self.assertEqual(points['2026-09-18']['net_worth'], 1200)
        self.assertEqual(points['2026-09-19']['net_worth'], 1210)
        self.assertEqual(points['2026-09-18']['valuation_key'], points['2026-09-19']['valuation_key'])
        self.assertIsNone(points['2026-09-16']['net_worth'])
        self.assertIsNone(points['2026-09-17']['net_worth'])
        self.assertEqual(history['continuity']['recorded_days'], 2, 'Repeated same-day fetches are one dated balance')
        self.assertEqual(history['continuity']['last_continuous_date'], '2026-09-19')
        self.assertEqual(history['continuity']['gaps'], first['history']['cohort']['continuity']['gaps'])
        self.assertIsNone(history['change'])


class WealthSavedCurrentBalanceIntegrationTests(unittest.TestCase):
    """Exercise real persistence and both value readers after account-detail GETs."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'finance.db'
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE accounts(id TEXT PRIMARY KEY, bank TEXT, account_type TEXT, display_name TEXT);
            CREATE TABLE statements(id INTEGER PRIMARY KEY, account_id TEXT, period_start TEXT, period_end TEXT,
                opening_balance REAL, closing_balance REAL, reconciled INTEGER, meta TEXT);
            CREATE TABLE reference_fx(date TEXT,currency TEXT,rate_to_gbp REAL,source TEXT,observed_date TEXT,
                fetched_at TEXT,source_url TEXT,PRIMARY KEY(date,currency));
        ''')
        for when in ('2026-08-30', '2026-08-31', '2026-09-18', '2026-09-19'):
            self.conn.execute('INSERT INTO reference_fx VALUES(?,?,?,?,?,?,?)',
                              (when, 'USD', .75, 'synthetic', when, when, None))
        self.conn.commit()
        replay = {'sources': {}}
        for identifier, values in [('usd', (750, 760)), ('gbp', (200, 205))]:
            replay['sources']['connected:' + identifier] = {
                'observations': [{'date': when, 'value': value, 'currency': 'GBP', 'source': 'reconstructed',
                                  'valuation_basis': 'daily_holdings', 'estimated': True, 'reconciled': True}
                                 for when, value in zip(('2026-08-30', '2026-08-31'), values)],
                'coverage': {}, 'issues': [],
            }
        self.replay = replay
        reconstruct = patch('app.investment_history.reconstruct', return_value=self.replay)
        reconstruct.start()
        self.addCleanup(reconstruct.stop)

    def save(self, receipt, values, *, known_clock=False, provider_history=()):
        accounts = []
        for identifier, code, value, minute in zip(('usd', 'gbp'), ('USD', 'GBP'), values, (23, 18)):
            row = account(identifier, value)
            row.update(currency=code if value is not None else None,
                       total_value_as_of=f'2026-09-18T09:{minute}:00Z' if known_clock else None,
                       holdings_synced_at=receipt[:10] + 'T17:00:00Z',
                       positions=snaptrade_models.section(),
                       balances=snaptrade_models.section([{'currency': code, 'cash': 50}]))
            row['history'] = snaptrade_models.section([
                {'date': when, 'currency': code, 'total_value': amount} for when, amount in provider_history])
            accounts.append(row)
        return snaptrade_store.save_portfolio({'configured': True, 'retrieved_at': receipt,
            'error': None, 'accounts': accounts, 'connections': snaptrade_models.section()}, path=self.path)

    def readers(self, today=TODAY):
        saved = snaptrade_store.load_portfolio(path=self.path)
        history = wealth.bundle(self.conn, path=self.path, today=today, range_name='6m',
                                account_ids=['connected:usd', 'connected:gbp'])
        analysis = investing_analytics.bundle(self.conn, path=self.path, today=today, range_name='6m',
                                              account_ids=['usd', 'gbp'], markets={})
        return saved, history, analysis

    def test_current_detail_receipt_replaces_daily_totals_in_all_readers_without_borrowing_a_clock(self):
        self.save('2026-09-18T17:07:00Z', (1000, 200), known_clock=True)
        old_rows = [tuple(row) for row in self.conn.execute('SELECT * FROM snaptrade_account_observations ORDER BY account_id')]
        self.save('2026-09-18T17:58:00Z', (900, 190))
        cached, data, analysis = self.readers()
        self.assertEqual([row['total_value'] for row in cached['accounts']], [900, 190])
        self.assertTrue(all(row['total_value_as_of'] is None for row in cached['accounts']))
        self.assertEqual(analysis['summary']['value'], 865)
        self.assertEqual(data['summary']['net_worth'], 865)
        self.assertEqual(data['history']['cohort']['end_value'], 865)
        self.assertEqual(data['history']['cohort']['end_date'], '2026-09-18')
        self.assertIsNone(analysis['balance_as_of'])
        for row in data['accounts']:
            self.assertEqual(row['as_of'], '2026-09-18T17:58:00Z')
            self.assertFalse(row['source_time_known'])
        current = snaptrade_store.list_account_observations(path=self.path)[-2:]
        self.assertTrue(all(row['observed_at'] == '2026-09-18T17:58:00Z' and not row['source_time_known'] for row in current))
        self.assertTrue(all(row['holdings_observed_at'] == '2026-09-18T17:00:00Z' for row in current))
        self.assertEqual([tuple(row) for row in self.conn.execute(
            'SELECT * FROM snaptrade_account_observations WHERE snapshot_id=1 ORDER BY account_id')], old_rows)
        missing = [point for point in data['history']['cohort']['points'] if '2026-09-01' <= point['date'] <= '2026-09-17']
        self.assertEqual(len(missing), 17)
        self.assertTrue(all(point['net_worth'] is None for point in missing))
        self.assertEqual(data['history']['cohort']['continuity']['recorded_days'], 1)
        self.assertFalse(data['history']['cohort']['change_comparable'])

    def test_next_day_current_receipt_extends_actual_history_without_repeating_daily_discovery_total(self):
        self.save('2026-09-18T17:07:00Z', (1000, 200), known_clock=True)
        self.save('2026-09-18T17:58:00Z', (900, 190))
        self.save('2026-09-19T17:58:00Z', (920, 195))
        self.save('2026-09-19T17:58:00Z', (920, 195))
        cached, data, analysis = self.readers(today=date(2026, 9, 19))
        self.assertEqual([row['total_value'] for row in cached['accounts']], [920, 195])
        self.assertEqual(analysis['summary']['value'], 885)
        self.assertEqual(data['summary']['net_worth'], 885)
        self.assertEqual(data['history']['cohort']['end_value'], 885)
        self.assertEqual(data['history']['cohort']['end_date'], '2026-09-19')
        self.assertEqual(len(snaptrade_store.list_snapshots(path=self.path)), 3)
        points = {point['date']: point for point in data['history']['cohort']['points']}
        self.assertEqual(points['2026-09-18']['net_worth'], 865)
        self.assertEqual(points['2026-09-19']['net_worth'], 885)
        self.assertEqual(points['2026-09-18']['valuation_key'], points['2026-09-19']['valuation_key'])
        self.assertIsNone(points['2026-09-17']['net_worth'])
        self.assertEqual(data['history']['cohort']['continuity']['recorded_days'], 2)
        self.assertEqual(data['history']['cohort']['continuity']['last_continuous_date'], '2026-09-19')
        self.assertEqual([(gap['start_date'], gap['end_date']) for gap in data['history']['cohort']['continuity']['gaps']],
                         [('2026-09-01', '2026-09-17')])
        self.assertFalse(data['history']['cohort']['change_comparable'])

    def test_explicitly_unknown_current_detail_total_does_not_resurrect_previous_daily_nav(self):
        self.save('2026-09-18T17:07:00Z', (1000, 200), known_clock=True)
        self.save('2026-09-18T17:58:00Z', (None, 190))
        cached, data, analysis = self.readers()
        self.assertIsNone(cached['accounts'][0]['total_value'])
        self.assertIsNone(cached['accounts'][0]['currency'])
        self.assertEqual(data['summary']['net_worth'], 190)
        self.assertEqual(data['summary']['missing_accounts'], 1)
        self.assertEqual(analysis['summary']['value'], 190)
        self.assertEqual(analysis['summary']['missing_accounts'], 1)
        missing = next(row for row in data['accounts'] if row['id'] == 'connected:usd')
        self.assertIsNone(missing['value'])
        self.assertFalse(missing['source_time_known'])
        self.assertEqual(missing['as_of'], '2026-09-18T17:58:00Z')

    def test_later_pdf_and_provider_history_cannot_replace_recorded_days_or_fill_future_gaps(self):
        self.save('2026-09-18T17:58:00Z', (900, 190))
        self.save('2026-09-20T17:58:00Z', (940, 200), provider_history=[
            ('2026-09-18', 8000), ('2026-09-19', 8100), ('2026-09-20', 8200)])
        for source in self.replay['sources'].values():
            source['observations'].extend({**source['observations'][0], 'date': when, 'value': 9900}
                                         for when in ('2026-09-17', '2026-09-18', '2026-09-19', '2026-09-20'))
        self.conn.execute("INSERT INTO accounts VALUES('later-pdf','Test broker','investment','Later PDF')")
        self.conn.execute('INSERT INTO statements VALUES(1,?,?,?,?,?,?,?)', (
            'later-pdf', '2026-09-01', '2026-09-19', None, None, 1,
            json.dumps({'parser': 'moomoo', 'base_currency': 'GBP', 'net_asset_value': 99999, 'native_reconciled': True})))
        accounts, _ = wealth.sources(self.conn, path=self.path, today=date(2026, 9, 20))
        source_reconciliation.set_link(self.conn, source_id='statement:later-pdf', canonical_id='connected:usd',
            evidence_kind='owner_confirmed', evidence_digest='f' * 64, accounts=accounts)
        self.conn.commit()
        original_replay = deepcopy(self.replay)
        original_statements = [tuple(row) for row in self.conn.execute('SELECT * FROM statements')]
        original_estimates = snaptrade_store.list_account_estimates(path=self.path)
        _, data, analysis = self.readers(today=date(2026, 9, 20))
        points = {point['date']: point for point in data['history']['cohort']['points']}
        self.assertEqual(points['2026-08-31']['net_worth'], 965, 'Verified older history remains available')
        self.assertEqual(points['2026-09-17']['net_worth'], 19800, 'A verified import may extend pre-collection history')
        self.assertEqual(points['2026-09-18']['net_worth'], 865)
        self.assertIsNone(points['2026-09-19']['net_worth'])
        self.assertEqual(points['2026-09-20']['net_worth'], 905)
        self.assertEqual(data['summary']['net_worth'], analysis['summary']['value'])
        self.assertEqual(data['history']['cohort']['end_value'], analysis['summary']['value'])
        self.assertEqual({row['recorded_only_from'] for row in data['history']['cohort']['account_coverage']}, {'2026-09-18'})
        self.assertEqual(data['history']['cohort']['continuity']['recorded_days'], 2)
        self.assertFalse(data['history']['cohort']['change_comparable'])
        self.assertEqual(self.replay, original_replay)
        self.assertEqual([tuple(row) for row in self.conn.execute('SELECT * FROM statements')], original_statements)
        self.assertEqual(snaptrade_store.list_account_estimates(path=self.path), original_estimates)
        self.assertEqual(len(original_estimates), 6, 'Excluded estimates stay inspectable in the source archive')

    def test_cutoff_uses_first_receipt_before_same_day_aggregation_and_ignores_chart_range(self):
        self.save('2026-09-18T10:00:00Z', (900, 190), known_clock=True)
        self.save('2026-09-20T17:58:00Z', (900, 190), known_clock=True,
                  provider_history=[('2026-09-19', 8100)])
        self.replay['sources'] = {}
        for range_name in ('1w', '6m'):
            with self.subTest(range=range_name):
                data = wealth.bundle(self.conn, path=self.path, today=date(2026, 9, 20), range_name=range_name,
                                    account_ids=['connected:usd', 'connected:gbp'])
                self.assertEqual({row['recorded_only_from'] for row in data['accounts']}, {'2026-09-18'})
                self.assertEqual(data['summary']['net_worth'], 865)
                daily = {point['date']: point for point in data['points']}
                self.assertIsNone(daily['2026-09-19']['net_worth'])
                self.assertEqual(data['history']['cohort']['points'], [], 'One dated source balance cannot create a fake daily trend')

    def test_recorded_only_grid_exposes_missing_day_without_any_reconstruction(self):
        self.replay['sources'] = {}
        self.save('2026-09-18T17:58:00Z', (900, 190))
        self.save('2026-09-20T17:58:00Z', (940, 200))
        _, data, _ = self.readers(today=date(2026, 9, 20))
        points = {point['date']: point for point in data['history']['cohort']['points']}
        self.assertEqual(set(points), {'2026-09-18', '2026-09-19', '2026-09-20'})
        self.assertEqual(points['2026-09-18']['net_worth'], 865)
        self.assertIsNone(points['2026-09-19']['net_worth'])
        self.assertEqual(points['2026-09-20']['net_worth'], 905)
        self.assertFalse(data['history']['cohort']['change_comparable'])

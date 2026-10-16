"""Synthetic investment review cases; no broker, private DB or market requests."""
from copy import deepcopy
from datetime import date, timedelta
import json
import sqlite3
import unittest
from unittest.mock import patch

from app import investment_insights as insights

TODAY = date(2026, 9, 18)


def market(returns=None):
    returns = returns if returns is not None else [.2, -.2] * 30 + [8]
    dates, when = [], TODAY
    while len(dates) <= len(returns):
        if when.weekday() < 5:
            dates.append(when.isoformat())
        when -= timedelta(days=1)
    dates.reverse()
    points, price = [{'date': dates[0], 'close': 100}], 100
    for when, change in zip(dates[1:], returns):
        price *= 1 + change / 100
        points.append({'date': when, 'close': price})
    return {'symbol': 'DEMO', 'currency': 'USD', 'exchange': 'NYQ', 'sector': 'Technology',
            'as_of': TODAY.isoformat(), 'status': 'ok', 'points': points}


def position(id='p1', instrument='i1', value=80, cost=50):
    return {'id': id, 'instrument_id': instrument, 'symbol': 'DEMO', 'name': 'Synthetic asset',
            'raw_symbol': 'DEMO', 'exchange': 'XNYS', 'listing_currency': 'USD',
            'asset_type': 'stock', 'currency': 'GBP', 'quantity': 1, 'price': value,
            'value': value, 'average_cost': cost, 'unrealized_pnl': value - cost if cost is not None else None,
            'is_cash': False}


def account(id='a1', positions=None, total=100, paper=False):
    return {'id': id, 'name': 'Test brokerage', 'institution': 'Test broker', 'connection_id': 'c1',
            'number_suffix': '1111', 'is_paper': paper, 'currency': 'GBP', 'total_value': total,
            'holdings_synced_at': TODAY.isoformat(),
            'positions': {'status': 'ok', 'data': [position()] if positions is None else positions},
            'balances': {'status': 'ok', 'data': [{'currency': 'GBP', 'cash': 20}]},
            'history': {'status': 'ok', 'data': [
                {'date': '2026-09-14', 'total_value': 99, 'currency': 'GBP'},
                {'date': '2026-09-16', 'total_value': 100, 'currency': 'GBP'},
                {'date': '2026-09-18', 'total_value': 100, 'currency': 'GBP'}]},
            'activities': {'status': 'empty', 'data': []}}


class InvestmentInsightsTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript('CREATE TABLE reference_fx(currency TEXT,date TEXT,rate_to_gbp REAL,observed_date TEXT);')
        self.conn.execute("INSERT INTO reference_fx VALUES('USD','2026-09-18',.75,'2026-09-18')")
        self.cached = {'accounts': [account()], 'connections': {'data': []}, 'cache': {}, 'retrieved_at': TODAY.isoformat()}
        self.markets = {'i1': market()}

    def result(self, **kwargs):
        return insights.bundle(self.conn, today=TODAY, cached=self.cached, markets=self.markets, **kwargs)

    def codes(self, result=None):
        return [item['code'] for item in (self.result() if result is None else result)['items']]

    def test_move_uses_prior_own_history_not_itself_or_price_levels(self):
        result = self.result()
        move = next(item for item in result['items'] if item['code'] == 'unusual_session_move')
        self.assertAlmostEqual(move['evidence'][0]['value'], 8)
        self.assertEqual(move['evidence'][2]['value'], 60)
        self.assertEqual(move['category'], 'movement')
        self.assertEqual(move['severity'], 'info')
        self.assertEqual(move['action']['position_id'], 'p1')
        self.assertEqual(result['coverage']['movement_eligible_instruments'], 1)
        self.assertFalse(result['coverage']['personal_profit_available'])
        self.assertNotIn('profit', move)
        self.markets['i1'] = market([6, -6] * 30 + [8])
        volatile = self.result()
        self.assertNotIn('unusual_session_move', self.codes(volatile))
        self.assertEqual(volatile['calibration']['absolute_moves_at_least_5pct'], 1)

    def test_short_history_zero_mad_materiality_and_discontinuous_latest_pair(self):
        for returns, expected in [([0] * 29 + [20], None), ([0] * 30 + [4], False), ([0] * 30 + [8], True)]:
            with self.subTest(returns=len(returns), latest=returns[-1]):
                result = insights.session_review(market(returns)['points'], TODAY)
                if expected is None:
                    self.assertIsNone(result)
                else:
                    self.assertEqual(result['unusual'], expected)
                    json.dumps(result, allow_nan=False)
        points = market()['points']
        points[-1]['date'] = '2026-10-18'
        self.assertIsNone(insights.session_review(points, date(2026, 10, 18)))

    def test_old_baselines_and_stale_or_mismatched_market_data_are_not_alerts(self):
        old_points = market()['points']
        for point in old_points[:-1]:
            point['date'] = (date.fromisoformat(point['date']) - timedelta(days=365)).isoformat()
        self.assertIsNone(insights.session_review(old_points, TODAY))
        for override in ({'status': 'error'}, {'exchange': 'TOR'}, {'currency': 'CAD'}):
            with self.subTest(override=override):
                self.markets['i1'] = {**market(), **override}
                result = self.result()
                self.assertNotIn('unusual_session_move', self.codes(result))
                self.assertEqual(result['coverage']['movement_eligible_instruments'], 0)
                self.assertIn('market_coverage', self.codes(result))

    def test_weekend_interval_qualifies_but_missing_weekday_does_not(self):
        points = market()['points']
        monday = date(2026, 9, 21)
        points[-1]['date'] = monday.isoformat()
        # Previous fixture point is Thursday: the missing Friday is not a
        # normal weekend, so a larger multiday move must not trigger this rule.
        self.assertIsNone(insights.session_review(points, monday))
        points[-2]['date'] = TODAY.isoformat()
        review = insights.session_review(points, monday)
        self.assertIsNotNone(review)
        self.assertEqual(review['observed_interval_days'], 3)

    def test_cost_basis_flag_does_not_call_zero_account_or_free_award_corrupt(self):
        self.cached['accounts'][0]['positions']['data'][0]['average_cost'] = 0
        self.cached['accounts'].append(account('a2', positions=[], total=0))
        result = self.result()
        cost = next(item for item in result['items'] if item['code'] == 'cost_basis_context')
        self.assertEqual(cost['severity'], 'info')
        self.assertIn('can be legitimate', cost['detail'])
        self.assertEqual(cost['evidence'][0]['value'], 1)
        self.assertNotIn('account_coverage', self.codes(result))
        self.assertEqual(result['coverage']['usable_cost_holdings'], 0)

    def test_one_group_per_missing_account_and_unknown_values_are_not_zero(self):
        missing = account('a2', positions=[], total=None)
        missing['positions']['status'] = 'unsupported'
        self.cached['accounts'].append(missing)
        result = self.result()
        flag = next(item for item in result['items'] if item['code'] == 'account_coverage')
        self.assertEqual(flag['account_ids'], ['a2'])
        self.assertEqual([item['value'] for item in flag['evidence']], [1, 1])
        self.assertEqual(result['coverage']['known_accounts'], 1)
        self.assertEqual(self.codes(result).count('account_coverage'), 1)
        self.assertIn('known accounts only', flag['detail'])

    def test_same_instrument_across_accounts_is_one_move_and_gross_exposure_uses_shorts(self):
        short = position('p2', value=-20)
        short['quantity'] = -1
        self.cached['accounts'].append(account('a2', positions=[short], total=None))
        self.cached['accounts'][0]['positions']['data'].append(position('p3', 'i2', value=100))
        self.markets['i2'] = market([.1] * 61)
        result = self.result()
        self.assertEqual(self.codes(result).count('unusual_session_move'), 1)
        move = next(item for item in result['items'] if item['code'] == 'unusual_session_move')
        self.assertEqual(move['account_ids'], ['a1', 'a2'])
        concentration = [item for item in result['items'] if item['code'] == 'known_security_concentration']
        self.assertEqual(len(concentration), 2)
        self.assertEqual([item['evidence'][0]['value'] for item in concentration], [50, 50])
        self.assertTrue(all(item['category'] == 'context' for item in concentration))
        self.assertEqual(result['calibration']['multiaccount_instruments'], 1)
        self.assertNotIn('duplicate_position_key', self.codes(result))

    def test_duplicate_position_grain_suppresses_exposure_and_move_but_same_ticker_does_not(self):
        self.cached['accounts'][0]['positions']['data'].append(deepcopy(position()))
        result = self.result()
        self.assertIn('duplicate_position_key', self.codes(result))
        self.assertNotIn('known_security_concentration', self.codes(result))
        self.assertNotIn('unusual_session_move', self.codes(result))
        self.cached['accounts'][0]['positions']['data'][-1] = position('p2', 'i2')
        self.markets['i2'] = market()
        result = self.result()
        self.assertNotIn('duplicate_position_key', self.codes(result))
        self.assertEqual(result['coverage']['unique_instruments'], 2)

    def test_concentration_drilldown_identifies_the_affected_holding(self):
        self.cached['accounts'].append(account('a2', positions=[position('p2')]))
        finding = next(item for item in self.result()['items'] if item['code'] == 'known_security_concentration')
        self.assertEqual(finding['account_ids'], ['a1', 'a2'])
        self.assertEqual(finding['holding_ids'], ['a1/p1', 'a2/p2'])
        self.assertEqual(finding['action'], {'target': 'holding', 'label': 'Inspect holding',
                                          'account_id': 'a1', 'position_id': 'p1'})

    def test_conflicts_across_unselected_accounts_withhold_move_and_concentration(self):
        other = position('p2')
        other['raw_symbol'] = 'OTHER'
        self.cached['accounts'].append(account('a2', positions=[other]))
        result = self.result(account_ids=['a1'])
        self.assertIn('instrument_identity_conflict', self.codes(result))
        self.assertNotIn('unusual_session_move', self.codes(result))
        self.assertNotIn('known_security_concentration', self.codes(result))
        self.assertNotIn('market_coverage', self.codes(result))

    def test_history_gaps_are_neutral_context_not_missing_days_claimed_as_profit(self):
        result = self.result()
        cadence = next(item for item in result['items'] if item['code'] == 'history_cadence')
        self.assertEqual(cadence['evidence'][0]['value'], 2)
        self.assertEqual(cadence['category'], 'context')
        self.assertEqual(cadence['action']['target'], 'calendar')
        self.assertFalse(result['coverage']['personal_profit_available'])
        self.assertEqual(result['summary']['review_count'], result['summary']['quality_count'] + result['summary']['movement_count'])

    def test_live_paper_selection_and_empty_selection_match_analytics_scope(self):
        self.cached['accounts'].append(account('paper', paper=True))
        self.assertEqual(self.result()['selected_account_ids'], ['a1'])
        self.assertEqual(self.result(paper=True)['selected_account_ids'], ['paper'])
        with self.assertRaises(ValueError):
            self.result(account_ids=['a1', 'paper'])
        with self.assertRaises(ValueError):
            self.result(account_ids=['unknown'])
        empty = self.result(account_ids=['__none__'])
        self.assertEqual(empty['items'], [])
        self.assertEqual(empty['summary']['review_count'], 0)
        self.assertIsNone(empty['calibration']['top1_known_gross_securities_pct'])

    def test_report_currency_never_changes_price_move_or_its_alert(self):
        sterling, dollars = self.result(), self.result(currency_code='USD')
        before = next(item for item in sterling['items'] if item['code'] == 'unusual_session_move')
        after = next(item for item in dollars['items'] if item['code'] == 'unusual_session_move')
        self.assertEqual(before, after)

    def test_no_provider_calls_and_stable_review_identifiers(self):
        with patch.object(insights.snaptrade_store, 'load_portfolio', return_value=self.cached) as portfolio, \
             patch.object(insights.market_cache, 'load_market_data', return_value=self.markets) as markets, \
             patch.object(insights.market_cache, 'fetch_listing', side_effect=AssertionError('No external calls')):
            first = insights.bundle(self.conn, today=TODAY)
            second = insights.bundle(self.conn, today=TODAY)
        self.assertEqual(first, second)
        self.assertEqual(portfolio.call_count, 2)
        self.assertEqual(markets.call_count, 2)
        json.dumps(first, allow_nan=False)
        self.cached['accounts'][0]['positions']['data'][0]['id'] = 'provider-reordered-position'
        reordered = self.result()
        self.assertEqual([item['id'] for item in first['items']], [item['id'] for item in reordered['items']])


if __name__ == '__main__':
    unittest.main()

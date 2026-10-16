"""Exposure denominators, identity checks, scope and static shock semantics."""
from copy import deepcopy
from datetime import date
import sqlite3
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import investment_exposure, investing_routes


def position(identifier, value, *, position_id=None, symbol=None):
    return {'id': position_id or identifier, 'instrument_id': identifier,
            'symbol': symbol or identifier, 'raw_symbol': symbol or identifier,
            'name': 'Synthetic ' + identifier, 'listing_currency': 'USD', 'exchange': 'XNYS',
            'asset_type': 'stock', 'currency': 'GBP', 'quantity': 1, 'value': value,
            'price': value, 'average_cost': 1, 'unrealized_pnl': 0, 'is_cash': False}


def account(identifier, positions):
    return {'id': identifier, 'name': identifier, 'institution': 'Synthetic', 'currency': 'GBP',
            'total_value': 100, 'holdings_synced_at': '2026-09-18', 'is_paper': False,
            'positions': {'status': 'ok', 'data': positions},
            'balances': {'status': 'ok', 'data': [{'currency': 'GBP', 'cash': 0}]}}


class ExposureTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.execute('CREATE TABLE reference_fx(currency TEXT,date TEXT,observed_date TEXT,rate_to_gbp REAL)')
        self.conn.execute("INSERT INTO reference_fx VALUES('USD','2026-09-18','2026-09-18',.75)")
        self.cached = {'accounts': [account('a', [position('ONE', 50), position('TWO', 50)]),
                                    account('b', [position('ONE', 100)])]}

    def data(self, **kwargs):
        return investment_exposure.bundle(self.conn, cached=self.cached, markets={}, today=date(2026, 9, 18), **kwargs)

    def test_exact_instruments_combine_and_math_uses_known_gross_securities(self):
        result = self.data()
        self.assertEqual(result['summary']['gross_security_value'], 200)
        self.assertEqual(result['summary']['largest_weight_pct'], 75)
        self.assertEqual(result['summary']['top5_weight_pct'], 100)
        self.assertEqual(result['summary']['hhi_fraction'], .625)
        self.assertEqual(result['summary']['effective_instruments'], 1.6)
        self.assertEqual(result['account_overlap'][0]['overlap_pct'], 50)
        self.assertEqual(result['account_overlap'][0]['shared_instruments'], 1)
        self.assertEqual(result['scenarios']['largest_gross_impact_pct'], -7.5)
        self.assertEqual(result['scenarios']['top5_value_change'], -20)
        self.assertEqual([a['name'] for a in result['instruments'][0]['accounts']], ['a', 'b'])
        self.assertTrue(result['instruments'][0]['identity_verified'])
        # Cash is excluded even if it substantially changes account NAV.
        self.cached['accounts'][0]['total_value'] = 10100
        self.cached['accounts'][0]['balances']['data'][0]['cash'] = 10000
        self.assertEqual(self.data()['summary'], result['summary'])

    def test_scope_and_reporting_currency(self):
        result = self.data(account_ids=['a'])
        self.assertEqual(result['summary']['hhi_fraction'], .5)
        self.assertEqual(result['account_overlap'], [])
        dollar = self.data(currency_code='USD')
        self.assertAlmostEqual(dollar['summary']['gross_security_value'], 200 / .75)
        self.assertAlmostEqual(dollar['summary']['hhi_fraction'], .625)
        paper = account('paper', [position('ONE', 500)])
        paper['is_paper'] = True
        self.cached['accounts'].append(paper)
        self.assertEqual(self.data()['summary']['gross_security_value'], 200)
        self.assertEqual(self.data(paper=True)['summary']['gross_security_value'], 500)
        with self.assertRaises(ValueError): self.data(account_ids=['a', 'paper'])
        with self.assertRaises(ValueError): self.data(account_ids=['absent'])

    def test_same_ticker_different_instruments_do_not_merge(self):
        self.cached['accounts'][1]['positions']['data'][0] = position('THREE', 100, symbol='ONE')
        result = self.data()
        self.assertEqual(result['summary']['instruments'], 3)
        self.assertEqual(result['account_overlap'][0]['overlap_pct'], 0)

    def test_conflict_in_deselected_account_invalidates_shared_instrument(self):
        self.cached['accounts'][1]['positions']['data'][0]['raw_symbol'] = 'OTHER'
        result = self.data(account_ids=['a'])
        self.assertIsNone(result['summary']['hhi_fraction'])
        self.assertIsNone(result['scenarios']['largest_gross_impact_pct'])
        self.assertEqual([row['instrument_id'] for row in result['instruments']], ['TWO'])

    def test_missing_identity_withholds_concentration_and_affected_overlap(self):
        self.cached['accounts'][0]['positions']['data'][1]['instrument_id'] = None
        result = self.data()
        self.assertIsNone(result['summary']['largest_weight_pct'])
        self.assertIsNone(result['account_overlap'][0]['overlap_pct'])
        self.assertTrue(result['warnings'])

    def test_missing_values_are_explicit_known_subset_and_overlap_unavailable(self):
        self.cached['accounts'][0]['positions']['data'][1]['value'] = None
        result = self.data()
        self.assertEqual(result['summary']['gross_security_value'], 150)
        self.assertEqual(result['summary']['known_holdings'], 2)
        self.assertEqual(result['summary']['hhi_fraction'], 1)
        self.assertIsNone(result['account_overlap'][0]['overlap_pct'])
        self.assertEqual(result['status'], 'partial')

    def test_duplicates_withhold_totals_but_distinct_lots_can_combine(self):
        self.cached['accounts'][0]['positions']['data'].append(deepcopy(self.cached['accounts'][0]['positions']['data'][0]))
        result = self.data()
        self.assertIsNone(result['summary']['gross_security_value'])
        self.assertEqual(result['instruments'], [])
        self.assertEqual(result['account_overlap'], [])
        self.cached['accounts'][0]['positions']['data'][-1]['id'] = 'other-lot'
        self.assertEqual(self.data()['summary']['gross_security_value'], 250)
        self.cached['accounts'].append(deepcopy(self.cached['accounts'][0]))
        self.assertIsNone(self.data()['summary']['gross_security_value'])

    def test_shorts_keep_gross_concentration_but_do_not_get_long_scenarios(self):
        row = self.cached['accounts'][0]['positions']['data'][0]
        row.update(value=-50, quantity=-1)
        result = self.data()
        self.assertEqual(result['summary']['gross_security_value'], 200)
        self.assertEqual(result['summary']['hhi_fraction'], .625)
        self.assertIsNone(result['scenarios']['largest_gross_impact_pct'])

    def test_zero_empty_and_incomplete_accounts(self):
        self.assertIsNone(self.data(account_ids=['__none__'])['summary']['hhi_fraction'])
        for a in self.cached['accounts']:
            for p in a['positions']['data']: p['value'] = 0
        result = self.data()
        self.assertEqual(result['summary']['gross_security_value'], 0)
        self.assertIsNone(result['summary']['effective_instruments'])
        self.assertEqual(result['account_overlap'], [])
        self.cached['accounts'][0]['positions']['status'] = 'unsupported'
        self.assertTrue(self.data()['warnings'])

    def test_read_only_bundle_without_cache_tables_has_no_network_or_writes(self):
        self.conn.execute('PRAGMA query_only=ON')
        with patch('app.snaptrade_store.load_portfolio', side_effect=AssertionError('must not open a write connection')), \
             patch('app.market_cache.load_market_data', side_effect=AssertionError('must not create cache tables')):
            result = investment_exposure.bundle(self.conn, today=date(2026, 9, 18))
        self.assertEqual(result['selected_account_ids'], [])

    def test_supported_cash_only_or_empty_account_has_zero_security_exposure(self):
        self.cached['accounts'] = [account('cash-only', [])]
        self.cached['accounts'][0]['positions']['status'] = 'empty'
        self.assertEqual(self.data()['summary']['gross_security_value'], 0)
        self.cached['accounts'][0]['total_value'] = 0
        self.assertEqual(self.data()['summary']['gross_security_value'], 0)
        self.assertIsNone(self.data()['summary']['hhi_fraction'])
        self.assertIsNone(self.data()['scenarios']['largest_gross_impact_pct'])
        for field, value in [('total_value', None), ('initial_sync_complete', False), ('holdings_unavailable', True)]:
            original = deepcopy(self.cached)
            self.cached['accounts'][0][field] = value
            self.assertIsNone(self.data()['summary']['gross_security_value'])
            self.cached = original
        self.cached['accounts'][0]['positions']['status'] = 'unsupported'
        self.assertIsNone(self.data()['summary']['gross_security_value'])
        self.assertIsNone(self.data(account_ids=['__none__'])['summary']['gross_security_value'])

    def test_endpoint_uses_private_cache_and_scope(self):
        app = FastAPI(); app.include_router(investing_routes.router)
        with TestClient(app) as client, patch.object(investing_routes, 'connection', return_value=Mock()), \
                patch.object(investment_exposure, 'bundle', return_value={'summary': {}}) as run:
            response = client.get('/api/investments/analysis/exposure?account=a&account=b&currency=USD')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(run.call_args.kwargs['account_ids'], ['a', 'b'])
        self.assertEqual(run.call_args.kwargs['currency_code'], 'USD')


if __name__ == '__main__':
    unittest.main()

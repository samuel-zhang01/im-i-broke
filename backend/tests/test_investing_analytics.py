"""Performance semantics using synthetic portfolios and public-price fixtures."""
from contextlib import closing
from copy import deepcopy
from datetime import date
import sqlite3
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import investing_analytics as analytics, investing_routes, snaptrade_store


TODAY = date(2026, 9, 18)


def holding(id='p1', instrument='instrument-one', *, quantity=2, code='GBP', value=165,
            asset='stock', cash=False):
    return {'id': id, 'instrument_id': instrument, 'symbol': 'SAME', 'name': 'Synthetic security',
            'raw_symbol': 'SAME', 'exchange': 'XNYS', 'listing_currency': 'USD',
            'asset_type': asset, 'currency': code, 'quantity': quantity, 'price': 82.5,
            'value': value, 'average_cost': 75, 'unrealized_pnl': 15, 'is_cash': cash}


def account(id='a1', *, paper=False, value=200, code='GBP', holdings=None, when='2026-09-18'):
    return {'id': id, 'name': 'Brokerage account', 'institution': 'Broker', 'connection_id': 'c1',
            'number_suffix': '4321', 'type': 'ISA', 'currency': code, 'total_value': value,
            'is_paper': paper, 'holdings_synced_at': when,
            'balances': {'status': 'ok', 'data': [{'cash': 35, 'buying_power': 999, 'currency': 'GBP'}]},
            'positions': {'status': 'ok', 'data': [holding()] if holdings is None else holdings}}


def market(*, code='USD', sector='Technology', status='ok'):
    return {'symbol': 'SAME', 'currency': code, 'sector': sector, 'status': status,
            'exchange': 'NYQ',
            'as_of': '2026-09-18T14:30:00Z', 'source_url': 'https://example.test/public-security',
            'points': [{'date': '2025-09-18', 'close': 50},
                       {'date': '2025-12-31', 'close': 55},
                       {'date': '2026-06-19', 'close': 75},
                       {'date': '2026-08-19', 'close': 80},
                       {'date': '2026-09-11', 'close': 90},
                       {'date': '2026-09-17', 'close': 100},
                       {'date': '2026-09-18', 'close': 110}]}


class InvestingAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript('''CREATE TABLE reference_fx(date TEXT,currency TEXT,
            rate_to_gbp REAL,source TEXT,observed_date TEXT,fetched_at TEXT,source_url TEXT,
            PRIMARY KEY(date,currency));''')
        self.conn.execute("INSERT INTO reference_fx VALUES('2026-09-18','USD',.75,'fixture','2026-09-18','2026-09-18',NULL)")
        self.cached = {'retrieved_at': '2026-09-18T14:00:00Z', 'cache': {},
                       'connections': {'data': []}, 'accounts': [account()]}
        self.markets = {'instrument-one': market()}

    def data(self, **kwargs):
        kwargs.setdefault('today', TODAY)
        return analytics.bundle(self.conn, cached=self.cached, markets=self.markets, **kwargs)

    def retained_portfolio(self, *, truncated=False):
        """Exercise the real cache/archive boundary with a failed second read."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / 'archive.db'
        self.conn.commit()
        with closing(sqlite3.connect(path)) as connection, connection:
            self.conn.backup(connection)
        raw = deepcopy(self.cached)
        raw.update(configured=True, retrieved_at='2026-09-19T05:06:42Z')
        raw['accounts'].append(account('a2'))
        for row in raw['accounts']:
            row['positions'].update(as_of='2026-09-19T05:06:41Z')
        raw['accounts'][1]['positions']['truncated'] = truncated
        snaptrade_store.save_portfolio(raw, path)
        failed = deepcopy(raw)
        failed['retrieved_at'] = '2026-09-19T05:17:01Z'
        failed['accounts'][0]['positions']['as_of'] = '2026-09-19T05:16:47Z'
        failed['accounts'][1]['holdings_synced_at'] = '2026-09-01T09:00:00Z'
        failed['accounts'][1]['positions'] = {'status': 'error', 'data': [], 'error': 'Timed out.'}
        self.cached = snaptrade_store.save_portfolio(failed, path)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)

    def test_value_includes_cash_once_and_day_move_uses_listing_currency(self):
        result = self.data()
        self.assertEqual(result['summary']['value'], 200)
        self.assertEqual(result['summary']['cash'], 35)
        self.assertEqual(result['summary']['day_move_estimate'], 15)
        self.assertEqual(result['summary']['unrealized_pnl'], 15)
        self.assertEqual(result['holdings'][0]['weight_pct'], 82.5)
        self.assertAlmostEqual(result['holdings'][0]['returns']['1d'], 10)
        self.assertAlmostEqual(result['holdings'][0]['returns']['1m'], 37.5)
        self.assertAlmostEqual(result['holdings'][0]['returns']['ytd'], 100)
        self.assertAlmostEqual(result['holdings'][0]['returns']['1y'], 120)
        self.assertEqual(result['holdings'][0]['native_price'], 82.5)
        self.assertEqual(result['holdings'][0]['market_currency'], 'USD')

    def test_reporting_currency_changes_values_but_never_stock_price_returns(self):
        sterling, dollars = self.data(), self.data(currency_code='USD')
        self.assertAlmostEqual(dollars['summary']['value'], 200 / .75)
        self.assertEqual(dollars['summary']['day_move_estimate'], 20)
        self.assertEqual(sterling['holdings'][0]['returns'], dollars['holdings'][0]['returns'])

    def test_balance_and_holdings_times_do_not_inherit_cache_retrieval_time(self):
        self.cached['accounts'][0].update(total_value_as_of='2026-09-18T09:23:00Z',
                                           holdings_synced_at='2026-09-18T13:30:00Z')
        self.cached['accounts'].append(account('a2', holdings=[holding('p2')]))
        self.cached['accounts'][1].update(total_value_as_of='2026-09-18T09:18:00Z',
                                           holdings_synced_at='2026-09-18T13:00:00Z')
        result = self.data()
        self.assertEqual(result['balance_as_of'], '2026-09-18T09:18:00Z')
        self.assertEqual(result['holdings_as_of'], '2026-09-18T13:00:00Z')
        self.assertEqual(result['retrieved_at'], '2026-09-18T14:00:00Z')
        self.cached['accounts'][1].pop('total_value_as_of')
        self.assertIsNone(self.data()['balance_as_of'])
        self.assertEqual(self.data(account_ids=['a1'])['balance_as_of'], '2026-09-18T09:23:00Z')
        self.cached['accounts'][1]['total_value_as_of'] = '2026-09-18T10:00:00+02:00'
        self.assertEqual(self.data()['balance_as_of'], '2026-09-18T10:00:00+02:00')
        self.cached['accounts'][1]['positions'].update(as_of='2026-09-17T12:00:00Z', stale=True)
        self.assertEqual(self.data()['holdings_as_of'], '2026-09-17T12:00:00Z')

    def test_missing_session_close_does_not_make_known_balances_or_positions_partial(self):
        self.markets['instrument-one']['points'][-2]['date'] = '2026-09-16'
        result = self.data()
        self.assertEqual(result['summary']['value'], 200)
        self.assertEqual(result['summary']['valued_holdings'], 1)
        self.assertEqual(result['summary']['pnl_holdings'], 1)
        self.assertEqual(result['summary']['priced_holdings'], 0)
        self.assertFalse(result['summary']['partial'])
        self.assertEqual(result['holdings'][0]['day_move_unavailable_reason'], 'missing_previous_session_close')
        self.assertIsNone(result['holdings'][0]['day_move_estimate'])

    def test_account_identity_and_instrument_identity_prevent_ticker_merging(self):
        self.cached['accounts'].append(account('a2', holdings=[holding('p2', 'different-listing', value=55)]))
        result = self.data()
        self.assertEqual(len(result['accounts']), 2)
        self.assertEqual(len(result['holdings']), 2)
        self.assertEqual(len({h['id'] for h in result['holdings']}), 2)
        second = result['holdings'][1]
        self.assertEqual(second['market_status'], 'unmatched')
        self.assertIsNone(second['returns']['1d'])
        selected = self.data(account_ids=['a2'])
        self.assertEqual(selected['summary']['value'], 200)
        self.assertEqual([h['account_id'] for h in selected['holdings']], ['a2'])
        self.assertEqual([a['selected'] for a in selected['accounts']], [False, True])

    def test_current_listing_must_still_match_cached_history_and_classification(self):
        for key, wrong in [('symbol', 'OTHER'), ('currency', 'CAD'), ('exchange', 'TOR')]:
            with self.subTest(key=key):
                self.markets['instrument-one'] = market()
                self.markets['instrument-one'][key] = wrong
                result = self.data()
                row = result['holdings'][0]
                self.assertEqual(row['market_status'], 'unmatched')
                self.assertEqual(row['sector'], 'Unclassified')
                self.assertIsNone(row['returns']['1d'])
                self.assertIsNone(row['day_move_estimate'])
                self.assertEqual(row['value'], 165)
                detail = analytics.security(self.conn, account_id='a1', position_id='p1', today=TODAY,
                                            markets=self.markets, cached=self.cached)
                self.assertEqual(detail['points'], [])
        self.markets['instrument-one'] = market()
        self.cached['accounts'][0]['positions']['data'][0].pop('exchange')
        self.assertEqual(self.data()['holdings'][0]['market_status'], 'unmatched')

    def test_conflicting_current_instrument_identity_withholds_both_accounts_and_detail(self):
        other = holding('p2')
        other['raw_symbol'] = 'OTHER'
        self.cached['accounts'].append(account('a2', holdings=[other]))
        for selected in (None, ['a1'], ['a2']):
            result = self.data(account_ids=selected)
            self.assertTrue(all(row['market_status'] == 'unmatched' for row in result['holdings']))
            self.assertTrue(all(row['sector'] == 'Unclassified' for row in result['holdings']))
            self.assertIsNone(result['summary']['day_move_estimate'])
        detail = analytics.security(self.conn, account_id='a1', position_id='p1', today=TODAY,
                                    markets=self.markets, cached=self.cached)
        self.assertEqual(detail['points'], [])

    def test_paper_accounts_are_separate_and_empty_selection_is_unknown(self):
        self.cached['accounts'].append(account('paper', paper=True, value=100000))
        self.assertEqual(self.data()['summary']['value'], 200)
        self.assertEqual(self.data(paper=True)['summary']['value'], 100000)
        explicit = self.data(account_ids=['paper'])
        self.assertTrue(explicit['paper'])
        self.assertTrue(any('simulated' in message for message in explicit['warnings']))
        with self.assertRaisesRegex(ValueError, 'separately'):
            self.data(account_ids=['a1', 'paper'])
        empty = self.data(account_ids=['__none__'])
        self.assertIsNone(empty['summary']['value'])
        self.assertIsNone(empty['summary']['day_move_estimate'])

    def test_missing_fx_does_not_become_zero_or_hide_listing_currency_returns(self):
        self.conn.execute('DELETE FROM reference_fx')
        self.cached['accounts'][0]['currency'] = 'USD'
        self.cached['accounts'][0]['positions']['data'][0]['currency'] = 'USD'
        result = self.data()
        self.assertIsNone(result['summary']['value'])
        self.assertIsNone(result['summary']['day_move_estimate'])
        self.assertIsNone(result['holdings'][0]['value'])
        self.assertIsNone(result['holdings'][0]['weight_pct'])
        self.assertAlmostEqual(result['holdings'][0]['returns']['1d'], 10)
        self.assertEqual(result['summary']['missing_accounts'], 1)
        self.assertTrue(result['summary']['partial'])

    def test_unknown_account_keeps_partial_sum_but_suppresses_allocation_weights(self):
        self.cached['accounts'].append(account('unknown', value=None, holdings=[]))
        result = self.data()
        self.assertEqual(result['summary']['value'], 200)
        self.assertEqual(result['summary']['missing_accounts'], 1)
        self.assertIsNone(result['holdings'][0]['weight_pct'])
        self.assertIsNone(result['sectors'][0]['weight_pct'])

    def test_stale_quantities_or_disabled_connections_withhold_daily_estimates(self):
        for change in ('age', 'disabled', 'section', 'truncated', 'initial_sync'):
            with self.subTest(change=change):
                old = deepcopy(self.cached)
                if change == 'age':
                    self.cached['accounts'][0]['holdings_synced_at'] = '2026-09-01'
                elif change == 'disabled':
                    self.cached['connections']['data'] = [{'id': 'c1', 'disabled': True}]
                elif change == 'section':
                    self.cached['accounts'][0]['positions']['stale'] = True
                elif change == 'initial_sync':
                    self.cached['accounts'][0]['initial_sync_complete'] = False
                else:
                    self.cached['accounts'][0]['positions']['truncated'] = True
                result = self.data()
                self.assertIsNone(result['summary']['day_move_estimate'])
                self.assertEqual(result['summary']['value'], 200)
                self.assertEqual(result['holdings'][0]['day_move_unavailable_reason'], 'stale_holdings')
                self.assertAlmostEqual(result['holdings'][0]['returns']['1d'], 10)
                self.assertTrue(result['summary']['partial'])
                self.cached = old

    def test_unrelated_feed_and_overall_refresh_failures_do_not_expire_fresh_quantities(self):
        for section in ('activities', 'orders', 'history', 'balances', None):
            with self.subTest(section=section):
                cached = deepcopy(self.cached)
                cached['cache'].update(stale=True, refresh_error='A later refresh failed.')
                if section:
                    cached['accounts'][0][section] = {'status': 'error', 'stale': True, 'data': []}
                untouched = deepcopy(cached)
                result = analytics.bundle(self.conn, today=TODAY, cached=cached, markets=self.markets)
                self.assertEqual(result['summary']['day_move_estimate'], 15)
                self.assertFalse(result['holdings'][0]['stale'])
                self.assertFalse(result['summary']['partial'])
                self.assertEqual(cached, untouched)  # Keep the visible upstream failure.

    def test_positions_source_clock_governs_age_and_split_checks(self):
        row = self.cached['accounts'][0]
        row['holdings_synced_at'] = '2026-09-01T09:00:00Z'
        row['positions']['as_of'] = '2026-09-18T14:00:00Z'
        self.markets['instrument-one']['splits'] = [{'date': '2026-09-17', 'numerator': 2, 'denominator': 1}]
        result = self.data()
        self.assertEqual(result['summary']['day_move_estimate'], 15)
        self.assertEqual(result['holdings_as_of'], '2026-09-18T14:00:00Z')
        row['holdings_synced_at'] = '2026-09-18T14:00:00Z'
        for source in ('2026-09-01T14:00:00Z', '2026-09-19T14:00:00Z'):
            row['positions']['as_of'] = source
            self.assertIsNone(self.data()['summary']['day_move_estimate'])

    def test_recent_complete_retained_snapshot_supports_weekend_saved_share_estimate(self):
        self.retained_portfolio()
        untouched = deepcopy(self.cached)
        result = self.data(today=date(2026, 9, 19))
        self.assertEqual(result['summary']['priced_holdings'], 2)
        self.assertEqual(result['summary']['day_move_estimate'], 30)
        self.assertFalse(result['summary']['partial'])
        self.assertEqual([row['using_saved_holdings'] for row in result['accounts']], [False, True])
        self.assertEqual(result['accounts'][1]['holdings_as_of'], '2026-09-19T05:06:41Z')
        self.assertEqual(result['accounts'][1]['positions_status'], 'error')
        self.assertTrue(any('complete saved holdings snapshot' in text for text in result['warnings']))
        self.assertEqual(self.cached, untouched)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM snaptrade_position_observations').fetchone()[0], 3)

    def test_retained_snapshot_needs_exact_complete_archive_and_recent_enabled_source(self):
        self.retained_portfolio()
        baseline = deepcopy(self.cached)
        for change in ('no_archive', 'no_source_time', 'quantity', 'instrument', 'missing_row',
                       'duplicate', 'truncated', 'disabled', 'initial_sync', 'old'):
            with self.subTest(change=change):
                self.cached = deepcopy(baseline)
                row = self.cached['accounts'][1]
                section = row['positions']
                if change == 'no_archive': section['cached_at'] = '2026-09-19T05:00:00Z'
                elif change == 'no_source_time': section['as_of'] = None
                elif change == 'quantity': section['data'][0]['quantity'] = 3
                elif change == 'instrument': section['data'][0]['instrument_id'] = 'other'
                elif change == 'missing_row': section['data'] = []
                elif change == 'duplicate': section['data'].append(deepcopy(section['data'][0]))
                elif change == 'truncated': section['truncated'] = True
                elif change == 'disabled':
                    row['connection_id'] = 'disabled'
                    self.cached['connections']['data'] = [{'id': 'disabled', 'disabled': True}]
                elif change == 'initial_sync': row['initial_sync_complete'] = False
                result = self.data(today=date(2026, 9, 22) if change == 'old' else date(2026, 9, 19))
                selected = next(a for a in result['accounts'] if a['id'] == 'a2')
                self.assertFalse(selected['using_saved_holdings'])
                self.assertIsNone(selected['day_move_estimate'])
                if change != 'old':
                    self.assertEqual(result['accounts'][0]['day_move_estimate'], 15)
                    healthy = self.data(today=date(2026, 9, 19), account_ids=['a1'])
                    self.assertFalse(healthy['summary']['partial'])

    def test_retained_truncated_snapshot_cannot_become_complete_after_a_failure(self):
        self.retained_portfolio(truncated=True)
        self.assertTrue(self.cached['accounts'][1]['positions']['truncated'])
        # Even a legacy cache that lost the flag must fail archive verification.
        self.cached['accounts'][1]['positions']['truncated'] = False
        result = self.data(today=date(2026, 9, 19))
        self.assertEqual(result['summary']['priced_holdings'], 1)
        self.assertFalse(result['accounts'][1]['using_saved_holdings'])
        self.assertIsNone(result['accounts'][1]['day_move_estimate'])

    def test_verified_saved_quantities_still_require_safe_share_units_and_recent_prices(self):
        self.retained_portfolio()
        for reason in ('split', 'prices'):
            with self.subTest(reason=reason):
                original = deepcopy(self.markets)
                if reason == 'split':
                    self.markets['instrument-one']['splits'] = [
                        {'date': '2026-09-19', 'numerator': 2, 'denominator': 1}]
                else:
                    self.markets['instrument-one']['status'] = 'error'
                result = self.data(today=date(2026, 9, 19))
                self.assertIsNone(result['accounts'][1]['day_move_estimate'])
                self.assertEqual(result['holdings'][1]['day_move_unavailable_reason'],
                                 'share_units_changed' if reason == 'split' else 'stale_prices')
                self.assertFalse(any('Some session estimates use' in text for text in result['warnings']))
                self.markets = original

    def test_old_market_data_and_failed_refresh_keep_returns_with_stale_label(self):
        self.markets['instrument-one']['status'] = 'error'
        result = self.data()
        self.assertEqual(result['holdings'][0]['market_status'], 'stale')
        self.assertIsNone(result['summary']['day_move_estimate'])
        self.assertAlmostEqual(result['holdings'][0]['returns']['1d'], 10)
        self.assertTrue(any('older market closes' in w for w in result['warnings']))

    def test_fund_issuer_sector_not_mistaken_for_lookthrough_and_cash_excluded(self):
        self.cached['accounts'][0]['positions']['data'].extend([
            holding('fund', 'fund-instrument', asset='etf'), holding('cash', cash=True)])
        result = self.data()
        self.assertEqual(result['summary']['holdings_count'], 2)
        self.assertEqual({s['name'] for s in result['sectors']}, {'Technology', 'Funds & ETFs'})
        self.assertEqual(result['summary']['classified_holdings'], 1)
        self.assertEqual(result['summary']['direct_holdings'], 1)
        self.assertEqual(result['summary']['fund_holdings'], 1)
        self.assertEqual(result['summary']['unclassified_holdings'], 0)
        self.assertIsNone(result['holdings'][2]['day_move_estimate'])
        self.assertEqual(result['holdings'][2]['sector'], 'Cash equivalents')

    def test_short_positions_keep_signed_move_and_inverse_unrealized_return(self):
        self.cached['accounts'][0]['positions']['data'] = [holding(quantity=-2, value=-165)]
        result = self.data()
        self.assertEqual(result['summary']['day_move_estimate'], -15)
        self.assertEqual(result['holdings'][0]['weight_pct'], -82.5)
        self.assertAlmostEqual(result['holdings'][0]['unrealized_pct'], -10)

    def test_derivative_prices_do_not_become_ordinary_share_performance(self):
        self.cached['accounts'][0]['positions']['data'] = [holding(asset='option')]
        result = self.data()
        self.assertEqual(result['holdings'][0]['market_status'], 'unsupported')
        self.assertIsNone(result['holdings'][0]['period_return_pct'])
        self.assertIsNone(result['summary']['day_move_estimate'])

    def test_sparse_history_does_not_invent_monthly_return_and_conflicts_removed(self):
        self.markets['instrument-one']['points'] = [
            {'date': '2026-08-01', 'close': 25}, {'date': '2026-09-17', 'close': 100},
            {'date': '2026-09-18', 'close': 110}, {'date': '2026-09-19', 'close': 150},
            {'date': '2026-08-19', 'close': 80}, {'date': '2026-08-19', 'close': 81},
            {'date': '2026-09-16', 'close': 0}, {'date': '2026-09-15', 'close': float('nan')},
        ]
        result = self.data()
        self.assertIsNone(result['holdings'][0]['returns']['1m'])
        self.assertAlmostEqual(result['holdings'][0]['returns']['1d'], 10)
        self.assertEqual(result['summary']['day_move_estimate'], 15)

    def test_latest_session_excludes_missing_sessions_but_accepts_weekends(self):
        for first, last, valid in [
            ('2026-09-17', '2026-09-18', True),
            ('2026-09-11', '2026-09-14', True),
            ('2026-09-16', '2026-09-18', False),
            ('2026-09-11', '2026-09-18', False),
            ('2026-09-11', '2026-09-15', False),
        ]:
            with self.subTest(first=first, last=last):
                self.markets['instrument-one']['points'] = [
                    {'date': first, 'close': 100}, {'date': last, 'close': 110}]
                result = self.data()
                row = result['holdings'][0]
                if valid:
                    self.assertAlmostEqual(row['returns']['1d'], 10)
                else:
                    self.assertIsNone(row['returns']['1d'])
                    self.assertIsNone(row['day_move_estimate'])
                    self.assertEqual(result['summary']['priced_holdings'], 0)

    def test_split_after_or_on_holdings_date_withholds_money_estimate_only(self):
        self.markets['instrument-one'].update(
            split_adjusted=True, split_events_complete=True,
            splits=[{'date': '2026-09-18', 'numerator': 2, 'denominator': 1}])
        for observed in ('2026-09-17T20:00:00Z', '2026-09-18T20:00:00Z'):
            with self.subTest(observed=observed):
                self.cached['accounts'][0]['holdings_synced_at'] = observed
                result = self.data()
                self.assertIsNone(result['summary']['day_move_estimate'])
                self.assertEqual(result['summary']['value'], 200)
                self.assertAlmostEqual(result['holdings'][0]['returns']['1d'], 10)
                self.assertTrue(any('stock split' in warning for warning in result['warnings']))
        self.cached['accounts'][0]['holdings_synced_at'] = '2026-09-18T14:00:00Z'
        self.markets['instrument-one']['splits'][0]['date'] = '2026-09-17'
        self.assertEqual(self.data()['summary']['day_move_estimate'], 15)
        self.assertFalse(any('stock split' in warning for warning in self.data()['warnings']))

    def test_detail_chart_retains_calendar_anchor_and_does_not_apply_current_quantity(self):
        detail = analytics.security(self.conn, account_id='a1', position_id='p1', today=TODAY,
                                    markets=self.markets, cached=self.cached)
        self.assertEqual(detail['points'][0], {'date': '2026-08-19', 'close': 80.0, 'return_pct': 0})
        self.assertAlmostEqual(detail['points'][-1]['return_pct'], 37.5)
        self.assertEqual(detail['points'][-1]['close'], 110)
        self.assertEqual(detail['market_currency'], 'USD')
        self.assertEqual(detail['price_basis'], 'price_return')
        self.assertNotIn('portfolio_value', detail['points'][0])
        with self.assertRaises(LookupError):
            analytics.security(self.conn, account_id='a1', position_id='not-a-position', today=TODAY,
                               markets=self.markets, cached=self.cached)

    def test_invalid_filters_and_identity_missing_cache_are_explicit(self):
        for kwargs in ({'account_ids': ['not-an-account']}, {'currency_code': 'GBp'}, {'range_name': '10y'}):
            with self.assertRaises(ValueError):
                self.data(**kwargs)
        self.cached['accounts'][0]['positions']['data'][0].pop('instrument_id')
        self.assertEqual(self.data()['holdings'][0]['market_status'], 'unmatched')

    def test_cached_reads_do_not_call_market_provider(self):
        with patch.object(analytics.snaptrade_store, 'load_portfolio', return_value=self.cached) as cache_read, \
             patch.object(analytics, '_load_markets', return_value=self.markets) as market_read:
            result = analytics.bundle(self.conn, path='synthetic.db', today=TODAY)
        self.assertEqual(result['summary']['value'], 200)
        cache_read.assert_called_once_with(path='synthetic.db')
        market_read.assert_called_once_with('synthetic.db')

    def test_security_loads_only_selected_account_listings_but_keeps_global_identity_checks(self):
        from app import market_cache
        self.cached['accounts'].append(account('a2', holdings=[holding('other-position', 'other-instrument')]))
        with patch.object(market_cache, 'load_market_data', return_value=self.markets) as read:
            result = analytics.security(self.conn, account_id='a1', position_id='p1', path='synthetic.db',
                                        today=TODAY, cached=self.cached)
        read.assert_called_once_with('synthetic.db', instrument_ids={'instrument-one'})
        self.assertTrue(result['points'])
        self.cached['accounts'][1]['positions']['data'][0].update(instrument_id='instrument-one', raw_symbol='OTHER')
        with patch.object(market_cache, 'load_market_data', return_value=self.markets):
            result = analytics.security(self.conn, account_id='a1', position_id='p1', path='synthetic.db',
                                        today=TODAY, cached=self.cached)
        self.assertFalse(result['points'])


class InvestingRouteTests(unittest.TestCase):
    def test_read_route_no_store_query_selection_and_invalid_detail(self):
        app = FastAPI()
        app.include_router(investing_routes.router)
        client = TestClient(app)
        self.addCleanup(client.close)
        connection = unittest.mock.Mock()
        with patch.object(investing_routes, 'connection', return_value=connection), \
             patch.object(analytics, 'bundle', return_value={'summary': {'value': None}}) as service:
            response = client.get('/api/investments/analysis?account=one&account=two&currency=USD&range=ytd')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(service.call_args.kwargs['account_ids'], ['one', 'two'])
            self.assertEqual(service.call_args.kwargs['range_name'], 'ytd')
            connection.close.assert_called_once()
        with patch.object(investing_routes, 'connection', return_value=connection), \
             patch.object(analytics, 'security', side_effect=LookupError('Holding unavailable')):
            self.assertEqual(client.get('/api/investments/analysis/security?account=one&position=p1').status_code, 404)


class InvestingAccessTests(unittest.TestCase):
    def setUp(self):
        from app import auth, main, market_routes
        self.auth, self.main, self.market_routes = auth, main, market_routes
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.auth_db = Path(folder.name) / 'auth.db'
        environment = patch.dict(os.environ, {
            'FINANCE_DEPLOYMENT': 'public', 'FINANCE_AUTH_ENABLED': 'true',
            'FINANCE_COOKIE_SECURE': 'true', 'FINANCE_PUBLIC_ORIGIN': 'https://testserver',
            'FINANCE_ALLOWED_HOSTS': 'testserver,localhost,127.0.0.1',
            'FINANCE_AUTH_DB_PATH': str(self.auth_db),
            'FINANCE_AUTH_KEY_PATH': str(Path(folder.name) / 'keys' / 'auth.key'),
        })
        environment.start()
        self.addCleanup(environment.stop)
        for target, attribute, value in (
            (main, 'DB_PATH', Path(folder.name) / 'unused-finance.db'),
            (investing_routes.db, 'DB_PATH', Path(folder.name) / 'unused-finance.db'),
        ):
            change = patch.object(target, attribute, value)
            change.start()
            self.addCleanup(change.stop)
        for name, change in (
            ('connection', patch.object(investing_routes, 'connection')),
            ('bundle', patch.object(analytics, 'bundle', return_value={'accounts': [], 'summary': {}})),
            ('security', patch.object(analytics, 'security', return_value={'holding': {}, 'points': []})),
            ('refresh', patch.object(market_routes, 'refresh_market_data', return_value={'updated': 0})),
            ('intraday_refresh', patch('app.intraday_market.refresh_intraday', return_value={'updated': 0})),
            ('calendar', patch.object(investing_routes.investing_calendar, 'bundle', return_value={'days': [], 'months': []})),
            ('insights', patch.object(investing_routes.investment_insights, 'bundle', return_value={'items': []})),
            ('benchmarks', patch.object(market_routes, 'load_market_data', return_value={})),
        ):
            setattr(self, name, change.start())
            self.addCleanup(change.stop)
        self.client = TestClient(main.app, base_url='https://testserver')
        self.addCleanup(self.client.close)

    def session(self):
        token = 'synthetic-investing-authenticated-session'
        now = time.time()
        with self.auth.database() as conn:
            conn.execute('INSERT OR REPLACE INTO sessions VALUES(?,?,?)', (self.auth.digest(token), now, now))
            conn.commit()
        self.client.cookies.set(self.auth.cookie_name(), token)

    def requests(self, headers=None):
        headers = headers or {}
        return [self.client.get('/api/investments/analysis', headers=headers),
                self.client.get('/api/investments/analysis/security?account=a1&position=p1', headers=headers),
                self.client.get('/api/investments/analysis/calendar', headers=headers),
                self.client.get('/api/investments/analysis/insights', headers=headers),
                self.client.get('/api/investments/market/benchmarks', headers=headers),
                self.client.post('/api/investments/market/refresh', headers={'X-Ledger-Request': '1', **headers})]

    def test_anonymous_and_forged_forwarding_cannot_read_or_refresh(self):
        for headers in ({}, {'X-Forwarded-For': '192.168.50.10', 'X-Real-IP': '127.0.0.1'}):
            for response in self.requests(headers):
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers['cache-control'], 'no-store')
        self.connection.assert_not_called()
        self.bundle.assert_not_called()
        self.security.assert_not_called()
        self.refresh.assert_not_called()
        self.intraday_refresh.assert_not_called()
        self.calendar.assert_not_called()
        self.insights.assert_not_called()
        self.benchmarks.assert_not_called()
        self.assertFalse(self.auth_db.exists())

    def test_authenticated_session_can_read_but_cross_site_cannot(self):
        self.session()
        for response in self.requests():
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertNotIn('access-control-allow-origin', response.headers)
        self.intraday_refresh.assert_called_once()
        for mocked in (self.connection, self.bundle, self.security, self.refresh, self.intraday_refresh,
                       self.calendar, self.insights, self.benchmarks):
            mocked.reset_mock()
        for response in self.requests({'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(response.status_code, 403)
        self.connection.assert_not_called()
        self.bundle.assert_not_called()
        self.security.assert_not_called()
        self.refresh.assert_not_called()
        self.intraday_refresh.assert_not_called()
        self.calendar.assert_not_called()
        self.insights.assert_not_called()
        self.benchmarks.assert_not_called()
        self.assertEqual(self.client.post('/api/investments/market/refresh').status_code, 403)

    def test_a_warm_private_response_still_requires_an_authenticated_session(self):
        with closing(sqlite3.connect(investing_routes.db.DB_PATH)) as conn, conn:
            conn.execute('CREATE TABLE synthetic(value INTEGER)')
        self.session()
        first = self.client.get('/api/investments/analysis')
        second = self.client.get('/api/investments/analysis')
        self.assertEqual(first.headers['x-ledger-investment-cache'], 'MISS')
        self.assertEqual(second.headers['x-ledger-investment-cache'], 'HIT')
        self.assertEqual(self.bundle.call_count, 1)
        self.client.cookies.clear()
        denied = self.client.get('/api/investments/analysis')
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(self.bundle.call_count, 1)

    def test_trusted_lan_remains_login_free_without_creating_auth_state(self):
        with patch.dict(os.environ, {'FINANCE_DEPLOYMENT': 'trusted-lan', 'FINANCE_AUTH_ENABLED': 'false',
                                   'FINANCE_COOKIE_SECURE': 'false', 'FINANCE_PUBLIC_ORIGIN': '',
                                   'FINANCE_BIND_ADDRESS': '192.168.50.10'}):
            for response in self.requests():
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['cache-control'], 'no-store')
                self.assertNotIn('set-cookie', response.headers)
        self.assertFalse(self.auth_db.exists())


if __name__ == '__main__':
    unittest.main()

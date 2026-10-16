"""Verified listing identity, unit conversion and durable failure behavior."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app import market_cache as market
from app import market_routes


def position(**changes):
    return dict(instrument_id='synthetic-us', raw_symbol='TEST', exchange='XNYS',
                listing_currency='USD', asset_type='stock', **changes)


def chart(symbol='TEST', currency='USD', exchange='NYQ', kind='EQUITY'):
    return {'chart': {'error': None, 'result': [{
        'meta': {'symbol': symbol, 'currency': currency, 'exchangeName': exchange,
                 'instrumentType': kind, 'regularMarketTime': 1725642000,
                 'exchangeTimezoneName': 'America/New_York'},
        'timestamp': [1725469200, 1725555600, 1725642000],
        'indicators': {'quote': [{'close': [100, 110, 120]}],
                       'adjclose': [{'adjclose': [90, 100, 115]}]},
    }]}}


class MarketCacheTests(unittest.TestCase):
    def test_refresh_retains_missing_close_only_on_same_split_and_listing_basis(self):
        expected = market.listing(position())
        previous = market.normalize_chart(chart(), expected, fetched_at='2024-09-06T20:00:00+00:00')
        raw = chart(); raw['chart']['result'][0]['indicators']['quote'][0]['close'][1] = None
        incoming = market.normalize_chart(raw, expected, fetched_at='2024-09-07T20:00:00+00:00')
        self.assertEqual(market.missing_previous_close(incoming['points']), '2024-09-05')
        filled = market.fill_price_gaps(incoming, previous)
        self.assertEqual(filled['points'], previous['points'])
        self.assertEqual(filled['retained_closes'], [{'date': '2024-09-05', 'fetched_at': previous['fetched_at']}])
        self.assertIsNone(market.missing_previous_close(filled['points']))
        for key, wrong in [('symbol', 'OTHER'), ('currency', 'CAD'), ('exchange', 'TOR'),
                           ('split_events_complete', False), ('dividends_included', True),
                           ('splits', [{'date': '2024-09-06', 'numerator': 2, 'denominator': 1}])]:
            changed = {**previous, key: wrong}
            self.assertEqual(market.fill_price_gaps(incoming, changed)['points'], incoming['points'])

    def test_recent_window_recovers_only_verified_missing_closes(self):
        primary = chart(); primary['chart']['result'][0]['indicators']['quote'][0]['close'][1] = None
        calls = []
        def read(url):
            calls.append(url)
            return chart() if 'range=5d' in url else primary if '/chart/' in url else {}
        value = market.fetch_listing(market.listing(position()), read=read)
        self.assertEqual([p['close'] for p in value['points']], [100, 110, 120])
        self.assertEqual(sum('range=5d' in url for url in calls), 1)
        # A mismatched fallback cannot relabel an instrument or invent a close.
        value = market.fetch_listing(market.listing(position()), read=lambda url: chart('OTHER') if 'range=5d' in url else primary if '/chart/' in url else {})
        self.assertEqual(len(value['points']), 2)

    def test_persisted_gap_repair_and_incomplete_refresh_receipt(self):
        portfolio = {'accounts': [{'positions': {'data': [position()]}}]}
        raw = chart(); raw['chart']['result'][0]['indicators']['quote'][0]['close'][1] = None
        missing = market.normalize_chart(raw, self.expected, fetched_at='2020-01-01T00:00:00+00:00')
        result = market.refresh_market_data(self.path, portfolio=portfolio, fetch=lambda _: missing, enforce_cooldown=False)
        self.assertEqual(result['incomplete'], 1)
        self.assertIn('partial', result['message'])
        complete = market.normalize_chart(chart(), self.expected, fetched_at='2020-01-02T00:00:00+00:00')
        market.refresh_market_data(self.path, portfolio=portfolio, fetch=lambda _: complete, enforce_cooldown=False)
        result = market.refresh_market_data(self.path, portfolio=portfolio, fetch=lambda _: missing, enforce_cooldown=False)
        saved = market.load_market_data(self.path)['synthetic-us']
        self.assertEqual(result['incomplete'], 0)
        self.assertEqual(len(saved['points']), 3)
        self.assertEqual(saved['retained_closes'][0]['fetched_at'], complete['fetched_at'])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'test.db'
        self.expected = market.listing(position())

    def test_listing_requires_instrument_venue_and_listing_currency(self):
        for key in ('instrument_id', 'raw_symbol', 'exchange', 'listing_currency'):
            row = position(); row[key] = None
            self.assertIsNone(market.listing(row))
        row = position(); row['listing_currency'] = 'CAD'
        self.assertIsNone(market.listing(row))
        row['listing_currency'] = 'USD'; row['asset_type'] = 'option'
        self.assertIsNone(market.listing(row))
        row = position(); row.update(raw_symbol='5', exchange='XHKG', listing_currency='HKD')
        self.assertEqual(market.listing(row)['symbol'], '0005.HK')
        row.update(raw_symbol='BRK.B', exchange='XNYS', listing_currency='USD')
        self.assertEqual(market.listing(row)['symbol'], 'BRK-B')

    def test_close_excludes_distributions_and_rejects_identity_mismatch(self):
        value = market.normalize_chart(chart(), self.expected)
        self.assertEqual([p['close'] for p in value['points']], [100, 110, 120])
        self.assertFalse(value['dividends_included'])
        self.assertTrue(value['split_adjusted'])
        for key, wrong in [('symbol', 'OTHER'), ('currency', 'CAD'), ('exchangeName', 'TOR'), ('instrumentType', 'ETF')]:
            raw = chart(); raw['chart']['result'][0]['meta'][key] = wrong
            with self.assertRaises(ValueError): market.normalize_chart(raw, self.expected)

    def test_pence_normalized_once_and_future_or_conflicting_data_rejected(self):
        expected = dict(self.expected, symbol='TEST.L', currency='GBP', exchanges={'LSE'})
        value = market.normalize_chart(chart('TEST.L', 'GBp', 'LSE'), expected)
        self.assertEqual(value['currency'], 'GBP')
        self.assertEqual(value['points'][-1]['close'], 1.2)
        raw = chart(); raw['chart']['result'][0]['meta']['regularMarketTime'] = 9999999999
        with self.assertRaises(ValueError): market.normalize_chart(raw, self.expected)

        raw = chart(); raw['chart']['result'][0]['timestamp'][1] = raw['chart']['result'][0]['timestamp'][0]
        with self.assertRaises(ValueError): market.normalize_chart(raw, self.expected)

    def test_split_events_survive_cache_and_invalid_ratio_is_rejected(self):
        raw = chart()
        raw['chart']['result'][0]['events'] = {'splits': {'one': {
            'date': 1725555600, 'numerator': 4, 'denominator': 1}}}
        value = market.normalize_chart(raw, self.expected)
        self.assertEqual(value['splits'], [{'date': '2024-09-05', 'numerator': 4, 'denominator': 1}])
        self.assertTrue(value['split_events_complete'])
        portfolio = {'accounts': [{'positions': {'data': [position()]}}]}
        market.refresh_market_data(self.path, portfolio=portfolio, fetch=lambda _: value, enforce_cooldown=False)
        self.assertEqual(market.load_market_data(self.path)['synthetic-us']['splits'], value['splits'])
        raw['chart']['result'][0]['events']['splits']['one']['denominator'] = 0
        with self.assertRaises(ValueError): market.normalize_chart(raw, self.expected)

    def test_optional_sector_failure_keeps_verified_history(self):
        def read(url):
            if '/chart/' in url: return chart()
            raise RuntimeError('synthetic secret must never be stored')
        self.assertEqual(len(market.fetch_listing(self.expected, read=read)['points']), 3)
        for response in ({'quotes': None}, {'quotes': [{'symbol': 'OTHER', 'exchange': 'NYQ', 'sector': 'Wrong'}]}):
            value = market.fetch_listing(self.expected, read=lambda url: chart() if '/chart/' in url else response)
            self.assertIsNone(value['sector'])

    def test_optional_profile_failure_retains_classification_with_its_original_time(self):
        portfolio = {'accounts': [{'positions': {'data': [position()]}}]}
        prior_time = '2020-01-01T00:00:00+00:00'
        prior = market.normalize_chart(chart(), self.expected, fetched_at=prior_time)
        prior.update(sector='Technology', industry='Software')
        market.refresh_market_data(self.path, portfolio=portfolio, fetch=lambda _: prior, enforce_cooldown=False)
        def read(url):
            if '/chart/' in url:
                value = chart()
                value['chart']['result'][0]['indicators']['quote'][0]['close'][-1] = 125
                return value
            raise RuntimeError('Optional profile unavailable')
        refreshed = market.fetch_listing(self.expected, read=read)
        market.refresh_market_data(self.path, portfolio=portfolio, fetch=lambda _: refreshed, enforce_cooldown=False)
        saved = market.load_market_data(self.path)['synthetic-us']
        self.assertEqual(saved['sector'], 'Technology')
        self.assertEqual(saved['industry'], 'Software')
        self.assertEqual(saved['sector_as_of'], prior_time)
        self.assertEqual(saved['industry_as_of'], prior_time)
        self.assertEqual(saved['fetched_at'], refreshed['fetched_at'])
        self.assertEqual(saved['points'][-1]['close'], 125)
        self.assertIsNone(saved['error'])

    def test_explicit_profile_changes_win_and_missing_fields_keep_separate_provenance(self):
        portfolio = {'accounts': [{'positions': {'data': [position()]}}]}
        prior = market.normalize_chart(chart(), self.expected, fetched_at='2020-01-01T00:00:00+00:00')
        prior.update(sector='Technology', industry='Software')
        market.refresh_market_data(self.path, portfolio=portfolio, fetch=lambda _: prior, enforce_cooldown=False)
        revised = market.normalize_chart(chart(), self.expected, fetched_at='2020-01-02T00:00:00+00:00')
        revised.update(sector='Financial Services', industry=None)
        market.refresh_market_data(self.path, portfolio=portfolio, fetch=lambda _: revised, enforce_cooldown=False)
        saved = market.load_market_data(self.path)['synthetic-us']
        self.assertEqual(saved['sector'], 'Financial Services')
        self.assertEqual(saved['sector_as_of'], revised['fetched_at'])
        self.assertEqual(saved['industry'], 'Software')
        self.assertEqual(saved['industry_as_of'], prior['fetched_at'])

    def test_changed_instrument_listing_currency_venue_or_kind_cannot_inherit_profile(self):
        base = market.normalize_chart(chart(), self.expected, fetched_at='2020-01-01T00:00:00+00:00')
        base.update(sector='Technology', industry='Software', asset_type='stock')
        for key, changed in [('instrument_id', 'other'), ('symbol', 'OTHER'),
                             ('currency', 'CAD'), ('exchange', 'TOR'), ('asset_type', 'adr'), ('status', 'unavailable')]:
            previous = {**base, key: changed}
            current = market.normalize_chart(chart(), self.expected)
            value = market._retain_classification(current, previous, self.expected, current['fetched_at'])
            with self.subTest(key=key):
                self.assertIsNone(value['sector'])
                self.assertIsNone(value['industry'])
        changed = position(); changed['raw_symbol'] = 'NEW'
        portfolio = {'accounts': [{'positions': {'data': [position()]}}]}
        market.refresh_market_data(self.path, portfolio=portfolio, fetch=lambda _: base, enforce_cooldown=False)
        market.refresh_market_data(self.path, portfolio={'accounts': [{'positions': {'data': [changed]}}]},
            fetch=lambda expected: market.normalize_chart(chart('NEW'), expected), enforce_cooldown=False)
        self.assertIsNone(market.load_market_data(self.path)['synthetic-us']['sector'])

    def test_failure_retains_prior_points_and_timestamp_without_raw_payloads(self):
        portfolio = {'accounts': [{'positions': {'data': [position()]}}]}
        fetched = market.normalize_chart(chart(), self.expected, fetched_at='2020-01-01T00:00:00+00:00')
        fetched['raw_payload'] = 'synthetic SECRET'
        first = market.refresh_market_data(self.path, portfolio=portfolio, fetch=lambda _: fetched, enforce_cooldown=False)
        self.assertEqual(first['updated'], 1)
        def fail(_): raise RuntimeError('synthetic SECRET')
        second = market.refresh_market_data(self.path, portfolio=portfolio, fetch=fail, enforce_cooldown=False)
        self.assertEqual(second['retained'], 1)
        loaded = market.load_market_data(self.path)['synthetic-us']
        self.assertEqual(loaded['points'], fetched['points'])
        self.assertEqual(loaded['fetched_at'], fetched['fetched_at'])
        self.assertTrue(loaded['error'])
        with closing(sqlite3.connect(self.path)) as conn, conn:
            self.assertNotIn('SECRET', '\n'.join(conn.iterdump()))

    def test_refresh_replaces_split_basis_and_deduplicates_across_accounts(self):
        portfolio = {'accounts': [{'positions': {'data': [position()]}}, {'positions': {'data': [position()]}}]}
        value = market.normalize_chart(chart(), self.expected, fetched_at='2020-01-01T00:00:00+00:00')
        market.refresh_market_data(self.path, portfolio=portfolio, fetch=lambda _: value, enforce_cooldown=False)
        revised = deepcopy(value); revised['points'] = [{'date': '2024-09-06', 'close': 60}]
        report = market.refresh_market_data(self.path, portfolio=portfolio, fetch=lambda _: revised, enforce_cooldown=False)
        self.assertEqual(report['attempted'], 1)
        self.assertEqual(market.load_market_data(self.path)['synthetic-us']['points'], revised['points'])

    def test_conflicting_provider_identity_is_not_sent_upstream(self):
        other = position(); other['raw_symbol'] = 'OTHER'
        portfolio = {'accounts': [{'positions': {'data': [position(), other]}}]}
        report = market.refresh_market_data(self.path, portfolio=portfolio,
            fetch=lambda _: self.fail('Conflicting identity fetched'), enforce_cooldown=False)
        self.assertEqual(report['attempted'], 0)
        self.assertEqual(report['unsupported'], 1)

    def test_fresh_cache_and_cooldown_avoid_repeated_network_calls(self):
        portfolio = {'accounts': [{'positions': {'data': [position()]}}]}
        value = market.normalize_chart(chart(), self.expected)
        market.refresh_market_data(self.path, portfolio=portfolio, fetch=lambda _: value, enforce_cooldown=False)
        report = market.refresh_market_data(self.path, portfolio=portfolio,
            fetch=lambda _: self.fail('Fresh cache fetched'), enforce_cooldown=False)
        self.assertEqual(report['attempted'], 0)
        with self.assertRaises(market.RefreshBusy):
            market.refresh_market_data(self.path, portfolio=portfolio)

    def test_index_history_is_verified_and_read_from_cache_without_network(self):
        definition = market.BENCHMARKS[0]
        normalized = market.normalize_chart(chart('^GSPC', 'USD', 'SNP', 'INDEX'), definition)
        self.assertEqual(normalized['instrument_id'], 'index:sp500')
        with patch.object(market_routes, 'load_market_data', return_value={'index:sp500': normalized}), \
             patch.object(market, 'fetch_listing', side_effect=AssertionError('Read called network')):
            payload = json.loads(market_routes.benchmarks(range='all').body)
        series = payload['series'][0]
        self.assertEqual(series['currency'], 'USD')
        self.assertEqual(series['points'][0]['return_pct'], 0)
        self.assertAlmostEqual(series['points'][-1]['return_pct'], 20)
        self.assertTrue(all(not item['points'] for item in payload['series'][1:]))

    def test_market_read_batches_queries_and_exact_filter_does_not_leak_other_histories(self):
        portfolio = {'accounts': [{'positions': {'data': [position()]}}]}
        value = market.normalize_chart(chart(), self.expected)
        market.refresh_market_data(self.path, portfolio=portfolio, fetch=lambda _: value, enforce_cooldown=False)
        statements = []
        conn = market.database(self.path)
        conn.set_trace_callback(statements.append)
        with patch.object(market, 'database', return_value=conn):
            loaded = market.load_market_data(self.path, instrument_ids=['synthetic-us'])
        self.assertEqual(sum(statement.startswith('SELECT ') for statement in statements), 2)
        self.assertEqual(set(loaded), {'synthetic-us'})
        self.assertEqual(loaded['synthetic-us']['points'], value['points'])
        self.assertEqual(market.load_market_data(self.path, instrument_ids=[]), {})
        self.assertEqual(market.load_market_data(self.path, instrument_ids=['not-a-listing']), {})

    def test_changed_listing_is_reverified_even_inside_fresh_cache_ttl(self):
        portfolio = {'accounts': [{'positions': {'data': [position()]}}]}
        value = market.normalize_chart(chart(), self.expected)
        market.refresh_market_data(self.path, portfolio=portfolio, fetch=lambda _: value, enforce_cooldown=False)
        changed = position(); changed['raw_symbol'] = 'NEW'
        fetched = []
        def read(expected):
            fetched.append(expected['symbol'])
            return market.normalize_chart(chart('NEW'), expected)
        market.refresh_market_data(self.path, portfolio={'accounts': [{'positions': {'data': [changed]}}]},
                                   fetch=read, enforce_cooldown=False)
        self.assertEqual(fetched, ['NEW'])
        self.assertEqual(market.load_market_data(self.path)['synthetic-us']['symbol'], 'NEW')


if __name__ == '__main__':
    unittest.main()

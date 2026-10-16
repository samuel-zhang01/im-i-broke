"""Hourly cache tests: verified listings, temporal availability and private I/O."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app import db, intraday_market as market, market_cache


def epoch(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def position(identifier='instrument-test', symbol='TEST'):
    return {'instrument_id': identifier, 'raw_symbol': symbol, 'exchange': 'XNYS',
            'listing_currency': 'USD', 'asset_type': 'stock', 'is_cash': False}


def chart(symbol='TEST', currency='USD', exchange='NYQ', kind='EQUITY'):
    start = epoch('2024-09-06T13:30:00Z')
    return {'chart': {'error': None, 'result': [{
        'meta': {'symbol': symbol, 'currency': currency, 'exchangeName': exchange,
                 'instrumentType': kind, 'regularMarketTime': start + 3 * 3600,
                 'exchangeTimezoneName': 'America/New_York', 'dataGranularity': '1h',
                 'tradingPeriods': [[{'start': start, 'end': start + 6.5 * 3600}]]},
        'timestamp': [start, start + 3600, start + 7200, start + 10800],
        'indicators': {'quote': [{'close': [100, 110, 120, 999]}],
                       'adjclose': [{'adjclose': [90, 100, 110, 999]}]},
    }]}}


class IntradayMarketTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'finance.db'
        self.expected = market_cache.listing(position())
        self.portfolio = {'accounts': [{'id': 'private-account', 'positions': {'data': [position()]}}]}
        patcher = patch.object(market, '_next_refresh', 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def load(self):
        connection = db.connect(self.path)
        try:
            return market.read_intraday(connection)
        finally:
            connection.close()

    def expire(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE investment_intraday_cache SET attempted_at='2000-01-01T00:00:00+00:00'")

    def test_completed_bar_close_becomes_available_at_end_not_start(self):
        result = market.normalize_chart(chart(), self.expected)
        self.assertEqual(len(result['points']), 3)
        self.assertEqual(result['points'][0], {'bar_start': '2024-09-06T13:30:00+00:00',
                                             'timestamp': '2024-09-06T14:30:00+00:00', 'close': 100})
        self.assertEqual(result['as_of'], '2024-09-06T16:30:00+00:00')
        self.assertEqual(result['market_as_of'], '2024-09-06T16:30:00+00:00')
        self.assertNotIn(999, [point['close'] for point in result['points']])
        self.assertTrue(result['split_adjusted'])
        self.assertFalse(result['dividends_included'])

    def test_partial_session_closes_at_session_end_and_last_tick_is_not_another_bar(self):
        raw = chart()
        row = raw['chart']['result'][0]
        end = epoch('2024-09-06T20:00:00Z')
        row['meta']['regularMarketTime'] = end
        row['timestamp'] = [epoch('2024-09-06T19:30:00Z'), end]
        row['indicators']['quote'][0]['close'] = [125, 999]
        result = market.normalize_chart(raw, self.expected)
        self.assertEqual(result['points'], [{'bar_start': '2024-09-06T19:30:00+00:00',
                                            'timestamp': '2024-09-06T20:00:00+00:00', 'close': 125}])

    def test_no_session_metadata_uses_conservative_full_hour_and_never_fills_closed_hours(self):
        raw = chart()
        row = raw['chart']['result'][0]
        row['meta'].pop('tradingPeriods')
        row['timestamp'] = [epoch('2024-09-05T13:30:00Z'), epoch('2024-09-06T15:30:00Z')]
        row['indicators']['quote'][0]['close'] = [95, 120]
        result = market.normalize_chart(raw, self.expected)
        self.assertEqual(len(result['points']), 2)
        self.assertEqual(result['points'][0]['timestamp'], '2024-09-05T14:30:00+00:00')
        self.assertEqual(result['points'][1]['timestamp'], '2024-09-06T16:30:00+00:00')

    def test_early_close_and_retrieval_clock_prevent_future_prices(self):
        raw = chart()
        row = raw['chart']['result'][0]
        end = epoch('2024-09-06T16:00:00Z')
        row['meta']['tradingPeriods'][0][0]['end'] = end
        row['meta']['regularMarketTime'] = end
        normalized = market.normalize_chart(raw, self.expected)
        self.assertEqual(normalized['points'][-1]['timestamp'], '2024-09-06T16:00:00+00:00')
        normalized = market.normalize_chart(raw, self.expected, fetched_at='2024-09-06T15:00:00Z')
        self.assertEqual(len(normalized['points']), 1)
        self.assertEqual(normalized['points'][-1]['timestamp'], '2024-09-06T14:30:00+00:00')

    def test_metadata_identity_currency_granularity_and_future_market_time_are_checked(self):
        for key, value in (('symbol', 'WRONG'), ('currency', 'CAD'), ('exchangeName', 'TOR'),
                           ('instrumentType', 'ETF'), ('dataGranularity', '1d'),
                           ('regularMarketTime', 9999999999), ('exchangeTimezoneName', 'Not/A/Zone')):
            with self.subTest(key=key):
                raw = chart()
                raw['chart']['result'][0]['meta'][key] = value
                with self.assertRaises(ValueError):
                    market.normalize_chart(raw, self.expected)
        expected = dict(self.expected, symbol='TEST.L', currency='GBP', exchanges={'LSE'})
        value = market.normalize_chart(chart('TEST.L', 'GBp', 'LSE'), expected)
        self.assertEqual(value['currency'], 'GBP')
        self.assertEqual(value['points'][0]['close'], 1)
        value = market.normalize_chart(chart('TEST.L', 'GBP', 'LSE'), expected)
        self.assertEqual(value['points'][0]['close'], 100)

    def test_bad_prices_unequal_series_conflicting_bars_and_old_window_are_not_accepted(self):
        raw = chart()
        row = raw['chart']['result'][0]
        row['indicators']['quote'][0]['close'] = [None, float('nan'), -1, 999]
        with self.assertRaises(ValueError):
            market.normalize_chart(raw, self.expected)
        row['indicators']['quote'][0]['close'] = [100]
        with self.assertRaises(ValueError):
            market.normalize_chart(raw, self.expected)
        raw = chart()
        row = raw['chart']['result'][0]
        row['timestamp'][1] = row['timestamp'][0]
        with self.assertRaises(ValueError):
            market.normalize_chart(raw, self.expected)
        raw = chart()
        row = raw['chart']['result'][0]
        row['meta'].pop('tradingPeriods')
        row['timestamp'][0] -= 40 * 86400
        result = market.normalize_chart(raw, self.expected)
        self.assertEqual(len(result['points']), 2)

    def test_fetch_url_contains_only_public_symbol_and_requested_intraday_parameters(self):
        requested = []
        def read(url):
            requested.append(url)
            return chart()
        value = market.fetch_listing(self.expected, read=read)
        self.assertTrue(value['points'])
        self.assertEqual(len(requested), 1)
        self.assertIn('/chart/TEST?range=1mo&interval=1h&events=splits&includePrePost=true', requested[0])
        self.assertNotIn('private-account', requested[0])
        self.assertNotIn('instrument-test', requested[0])

    def test_cache_ttl_deduplicates_accounts_and_failed_reads_retain_actual_timestamps(self):
        portfolio = deepcopy(self.portfolio)
        portfolio['accounts'].append(deepcopy(portfolio['accounts'][0]))
        value = market.normalize_chart(chart(), self.expected)
        value['raw_payload'] = 'synthetic-private-SECRET'
        report = market.refresh_intraday(self.path, portfolio=portfolio, fetch=lambda _: value, enforce_cooldown=False)
        self.assertEqual(report['attempted'], 1)
        report = market.refresh_intraday(self.path, portfolio=portfolio, fetch=lambda _: self.fail('Fresh prices refetched'), enforce_cooldown=False)
        self.assertEqual(report['attempted'], 0)
        self.assertEqual(report['skipped'], 1)
        self.expire()
        def failure(_):
            raise RuntimeError('synthetic-private-SECRET')
        report = market.refresh_intraday(self.path, portfolio=portfolio, fetch=failure, enforce_cooldown=False)
        self.assertEqual(report['retained'], 1)
        loaded = self.load()['instrument-test']
        self.assertEqual(loaded['points'], value['points'])
        self.assertEqual(loaded['as_of'], value['as_of'])
        self.assertEqual(loaded['fetched_at'], value['fetched_at'])
        self.assertEqual(loaded['status'], 'stale')
        self.assertTrue(loaded['error'])
        self.assertNotIn('SECRET', json.dumps(loaded))
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertNotIn('SECRET', '\n'.join(connection.iterdump()))
        report = market.refresh_intraday(self.path, portfolio=portfolio, fetch=lambda _: self.fail('Failed listing hammered'), enforce_cooldown=False)
        self.assertEqual(report['attempted'], 0)

    def test_changed_or_conflicting_listing_identity_never_reuses_old_prices(self):
        value = market.normalize_chart(chart(), self.expected)
        market.refresh_intraday(self.path, portfolio=self.portfolio, fetch=lambda _: value, enforce_cooldown=False)
        other = position(symbol='OTHER')
        market.refresh_intraday(self.path, portfolio={'accounts': [{'positions': {'data': [other]}}]},
                                fetch=lambda _: None, enforce_cooldown=False)
        cached = self.load()['instrument-test']
        self.assertEqual(cached['status'], 'unavailable')
        self.assertFalse(cached['identity_verified'])
        self.assertEqual(cached['points'], [])
        self.expire()
        market.refresh_intraday(self.path, portfolio=self.portfolio, fetch=lambda _: value, enforce_cooldown=False)
        report = market.refresh_intraday(self.path, portfolio={'accounts': [{'positions': {'data': [position(), other]}}]},
                                        fetch=lambda _: self.fail('Conflicting identity sent upstream'), enforce_cooldown=False)
        self.assertEqual(report['unsupported'], 1)
        self.assertEqual(self.load()['instrument-test']['points'], [])

    def test_missing_and_readonly_schema_are_pure_reads_and_filters_are_exact(self):
        connection = db.connect(self.path)
        try:
            connection.execute('PRAGMA query_only=ON')
            self.assertEqual(market.read_intraday(connection), {})
            self.assertFalse(connection.in_transaction)
        finally:
            connection.close()
        value = market.normalize_chart(chart(), self.expected)
        market.refresh_intraday(self.path, portfolio=self.portfolio, fetch=lambda _: value, enforce_cooldown=False)
        connection = db.connect(self.path)
        try:
            connection.execute('PRAGMA query_only=ON')
            self.assertEqual(set(market.read_intraday(connection, instrument_ids=['instrument-test'])), {'instrument-test'})
            self.assertEqual(market.read_intraday(connection, instrument_ids=['unknown']), {})
            self.assertEqual(market.read_intraday(connection, instrument_ids=[]), {})
            connection.execute('BEGIN')
            market.read_intraday(connection)
            self.assertTrue(connection.in_transaction)
        finally:
            connection.close()

    def test_failed_initial_read_is_explicit_and_refresh_cooldown_and_lock_are_enforced(self):
        report = market.refresh_intraday(self.path, portfolio=self.portfolio, fetch=lambda _: None)
        self.assertEqual(report['failed'], 1)
        cached = self.load()['instrument-test']
        self.assertEqual(cached['status'], 'unavailable')
        self.assertEqual(cached['points'], [])
        self.assertIsNone(cached['as_of'])
        with self.assertRaises(market.RefreshBusy):
            market.refresh_intraday(self.path, portfolio=self.portfolio)
        market._lock.acquire()
        try:
            with self.assertRaises(market.RefreshBusy):
                market.refresh_intraday(self.path, portfolio=self.portfolio, enforce_cooldown=False)
        finally:
            market._lock.release()

    def test_maximum_four_workers_and_unsupported_listings_are_not_sent(self):
        rows = [position('instrument-' + str(i), 'TEST' + str(i)) for i in range(8)]
        rows.append(dict(position('unsupported'), asset_type='option'))
        current = maximum = 0
        lock = threading.Lock()
        def fetch(expected):
            nonlocal current, maximum
            with lock:
                current += 1
                maximum = max(maximum, current)
            try:
                time.sleep(.01)
                return market.normalize_chart(chart(symbol=expected['symbol']), expected)
            finally:
                with lock:
                    current -= 1
        report = market.refresh_intraday(self.path, portfolio={'accounts': [{'positions': {'data': rows}}]},
                                        fetch=fetch, enforce_cooldown=False)
        self.assertEqual(report['updated'], 8)
        self.assertEqual(report['unsupported'], 1)
        self.assertLessEqual(maximum, 4)
        self.assertGreater(maximum, 1)

    def test_atomic_window_replacement_keeps_split_metadata_and_rolls_back_on_bad_write(self):
        raw = chart()
        raw['chart']['result'][0]['events'] = {'splits': {'event': {
            'date': epoch('2024-09-05T13:30:00Z'), 'numerator': 4, 'denominator': 1}}}
        value = market.normalize_chart(raw, self.expected)
        market.refresh_intraday(self.path, portfolio=self.portfolio, fetch=lambda _: value, enforce_cooldown=False)
        self.assertEqual(self.load()['instrument-test']['splits'][0]['numerator'], 4)
        self.expire()
        bad = deepcopy(value)
        bad['points'][0]['close'] = None
        with self.assertRaises(sqlite3.IntegrityError):
            market.refresh_intraday(self.path, portfolio=self.portfolio, fetch=lambda _: bad, enforce_cooldown=False)
        self.assertEqual(self.load()['instrument-test']['points'], value['points'])
        replacement = deepcopy(value)
        replacement['points'] = [value['points'][-1]]
        market.refresh_intraday(self.path, portfolio=self.portfolio, fetch=lambda _: replacement, enforce_cooldown=False)
        self.assertEqual(self.load()['instrument-test']['points'], replacement['points'])


if __name__ == '__main__':
    unittest.main()

class ExtendedTradingTests(unittest.TestCase):
    def raw(self, interval='1h'):
        raw = chart()
        row = raw['chart']['result'][0]
        meta = row['meta']
        regular = meta['tradingPeriods']
        pre_start = epoch('2024-09-06T08:00:00Z')
        meta.update(dataGranularity=interval, regularMarketTime=epoch('2024-09-05T20:00:00Z'), regularMarketPrice=99,
                    tradingPeriods={'regular': regular, 'pre': [[{'start': pre_start, 'end': epoch('2024-09-06T13:30:00Z')}]],
                                    'post': [[{'start': epoch('2024-09-06T20:00:00Z'), 'end': epoch('2024-09-07T00:00:00Z')}]]})
        row['timestamp'] = [epoch('2024-09-06T10:00:00Z'), epoch('2024-09-06T10:05:00Z'), epoch('2024-09-06T10:10:00Z')]
        row['indicators']['quote'] = [{'close': [101, 102, 999], 'volume': [100, 100, 100]}]
        return raw

    def test_extended_prices_continue_beyond_previous_regular_close_without_incomplete_bars(self):
        raw = self.raw()
        row = raw['chart']['result'][0]
        row['timestamp'] = [epoch('2024-09-06T09:00:00Z'), epoch('2024-09-06T10:00:00Z')]
        row['indicators']['quote'][0]['close'] = [101, 999]
        value = market.normalize_chart(raw, market_cache.listing(position()), include_extended=True, fetched_at='2024-09-06T10:12:00Z')
        self.assertEqual([p['close'] for p in value['points']], [101])
        self.assertEqual(set(value['point_sessions'].values()), {'pre-market'})

    def test_after_hours_crossing_midnight_uses_session_end(self):
        raw = self.raw()
        row=raw['chart']['result'][0]
        row['timestamp']=[epoch('2024-09-06T23:00:00Z')]
        row['indicators']['quote'][0]['close']=[105]
        value=market.normalize_chart(raw, market_cache.listing(position()), include_extended=True, fetched_at='2024-09-07T00:00:00Z')
        self.assertEqual(value['points'][0]['timestamp'], '2024-09-07T00:00:00+00:00')
        self.assertEqual(list(value['point_sessions'].values()), ['after-hours'])

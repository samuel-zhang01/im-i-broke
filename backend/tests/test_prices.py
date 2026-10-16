"""Public-provider contracts: identity, quote units, timestamps and dated FX."""
import io
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from app import prices
from app.db import init_db


def chart(symbol='CUKX.L', currency='GBp', price=21995, **changes):
    meta = {'symbol': symbol, 'currency': currency, 'regularMarketPrice': price,
            'regularMarketTime': 1789486200, 'exchangeName': 'LSE',
            'instrumentType': 'ETF', 'longName': 'Test security', **changes}
    return {'chart': {'result': [{'meta': meta}], 'error': None}}


class PublicPriceTests(unittest.TestCase):
    def setUp(self):
        prices._fx_cache.clear()
        prices._cooldowns.clear()
        prices._local.error = None
        self.network = patch('app.prices.urlopen', side_effect=AssertionError('Unexpected network'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_cukx_pence_quote_scales_once_and_ignores_unverified_cached_symbol(self):
        with patch('app.prices._get_json', return_value=chart()) as get:
            quote = prices.quote('IE00B53HP851', symbol='WRONG.L', ticker='CUKX', price_currency='GBX')
        self.assertEqual((quote['symbol'], quote['price_native']), ('CUKX.L', 21995))
        self.assertAlmostEqual(quote['price_gbp'], 219.95, places=8)
        self.assertEqual((quote['currency'], quote['fx_rate_to_gbp'], quote['fx_asof']), ('GBp', .01, None))
        self.assertEqual(quote['quote_asof'], '2026-09-15T15:30:00+00:00')
        self.assertNotEqual(quote['fetched_at'], quote['quote_asof'])
        self.assertNotIn('WRONG', get.call_args.args[0])
        self.assertEqual(json.loads(quote['quote_meta'])['resolution']['basis'], 'issuer_listing')

    def test_verified_listing_rejects_mislabeled_pounds_and_wrong_exchange(self):
        for response in [chart(currency='GBP'), chart(exchangeName='NYQ')]:
            with self.subTest(response=response), patch('app.prices._get_json', return_value=response):
                self.assertIsNone(prices.quote('IE00B53HP851', ticker='CUKX'))

    def test_vuag_uses_actual_pounds_listing_without_dividing_by_100(self):
        with patch('app.prices._get_json', return_value=chart('VUAG.L', 'GBP', 108.82)):
            result = prices.quote('IE00BFMXXD54', ticker='VUAG', price_currency='GBP')
        self.assertEqual((result['symbol'], result['price_gbp']), ('VUAG.L', 108.82))
        with patch('app.prices._get_json', return_value=chart('VUAG.L', 'GBp', 108.82)):
            self.assertIsNone(prices.quote('IE00BFMXXD54', ticker='VUAG', price_currency='GBP'))

    def test_distinct_currency_lines_and_cross_listings_follow_execution_hints(self):
        cases = [('IE00BFMXXD54', 'VUAA', 'USD', 'VUAA.L'),
                 ('XS2399364582', '5SPY', 'GBX', '5SPY.L'),
                 ('XS2399364582', 'SP5Y', 'USD', 'SP5Y.L'),
                 ('CA0636711016', 'BMO', 'USD', 'BMO'),
                 ('CA0636711016', 'BMO', 'CAD', 'BMO.TO')]
        for isin, ticker, currency, expected in cases:
            with self.subTest(ticker=ticker, currency=currency):
                self.assertEqual(prices.resolve_symbol(isin, ticker, currency), expected)
        self.assertIsNone(prices.resolve_symbol('IE00BFMXXD54', 'VUAG', 'USD'))

    def test_ambiguous_or_unrelated_isin_search_is_rejected(self):
        for response in [
            {'quotes': [{'symbol': 'OTHER', 'quoteType': 'EQUITY', 'exchange': 'NMS'}]},
            {'quotes': [{'symbol': 'AMD', 'quoteType': 'EQUITY', 'exchange': 'NMS'},
                        {'symbol': 'AMD.DE', 'quoteType': 'EQUITY', 'exchange': 'GER'}]},
            {'quotes': [{'symbol': 'AMD', 'quoteType': 'OPTION', 'exchange': 'NMS'}]},
            {'quotes': None},
        ]:
            with self.subTest(response=response), patch('app.prices._get_json', return_value=response):
                self.assertIsNone(prices.resolve_symbol('US0079031078', 'AMD', 'USD'))

    def test_missing_and_corrupt_provider_fields_never_produce_a_price(self):
        cases = [chart(price=float('inf')), chart(price=10**400), chart(price=-1), chart(price=True),
                 chart(currency=None), chart(symbol='OTHER'), chart(regularMarketTime=None),
                 chart(regularMarketTime=99999999999), {'chart': {'result': []}}, None]
        for response in cases:
            with self.subTest(response=response), patch('app.prices._get_json', return_value=response):
                self.assertIsNone(prices.fetch_market_quote('CUKX.L'))

    def test_fx_inverse_preserves_provider_time_and_rejects_wrong_pair_metadata(self):
        response = chart('GBPUSD=X', 'USD', 1.25, exchangeName='CCY', instrumentType='CURRENCY')
        with patch('app.prices._get_json', return_value=response):
            result = prices.current_fx_to_gbp('USD')
        self.assertEqual(result['rate_to_gbp'], .8)
        self.assertEqual(result['asof'], '2026-09-15T15:30:00+00:00')
        prices._fx_cache.clear()
        with patch('app.prices._get_json', return_value=chart('GBPUSD=X', 'GBP', 1.25, exchangeName='CCY', instrumentType='CURRENCY')):
            self.assertIsNone(prices.current_fx_to_gbp('USD'))

    def test_failed_batch_is_isolated_and_errors_are_preserved(self):
        def quote(isin, **_):
            if isin == 'BAD':
                raise OSError('Network failure')
            return {'isin': isin, 'price_gbp': 1}
        with patch('app.prices.quote', side_effect=quote):
            result = prices.batch_quote([{'isin': 'GOOD'}, {'isin': 'BAD'}])
        self.assertEqual(list(result), ['GOOD'])
        self.assertEqual(result.errors, {'BAD': 'quote_unavailable'})

    def test_rate_limit_cooldown_prevents_immediate_retry(self):
        error = urllib.error.HTTPError('https://query1.finance.yahoo.com/data', 429, 'Limited', {'Retry-After': '120'}, None)
        with patch('app.prices.time.monotonic', return_value=1000), patch('app.prices.urlopen', side_effect=error) as get:
            self.assertIsNone(prices._get_json('https://query1.finance.yahoo.com/data'))
            self.assertIsNone(prices._get_json('https://query1.finance.yahoo.com/other'))
        self.assertEqual(get.call_count, 1)
        self.assertEqual(prices._local.error, 'provider_rate_limited')
        self.assertEqual(prices._cooldowns['query1.finance.yahoo.com'], 1120)

    def test_oversized_json_is_rejected_before_parsing(self):
        with patch('app.prices.urlopen', return_value=io.BytesIO(b' ' * 2_000_001)):
            self.assertIsNone(prices._get_json('https://query1.finance.yahoo.com/data'))
        self.assertEqual(prices._local.error, 'provider_response_too_large')

    def test_dated_ecb_fx_is_rebased_and_carries_only_previous_publication(self):
        rows = [{'date': '2026-01-02', 'base': 'USD', 'quote': 'GBP', 'rate': .74},
                {'date': '2026-01-05', 'base': 'USD', 'quote': 'GBP', 'rate': .75}]
        with patch('app.prices._get_json', return_value=rows) as get:
            result = prices.batch_reference_fx([('USD', '2026-01-03'), ('USD', '2026-01-04'), ('USD', '2026-01-05')])
        self.assertEqual(get.call_count, 1)
        self.assertEqual(result[('USD', '2026-01-04')]['observed_date'], '2026-01-02')
        self.assertEqual(result[('USD', '2026-01-04')]['rate_to_gbp'], .74)
        self.assertEqual(result[('USD', '2026-01-05')]['rate_to_gbp'], .75)
        query = parse_qs(urlsplit(get.call_args.args[0]).query)
        self.assertEqual((query['base'], query['quotes'], query['providers']), (['USD'], ['GBP'], ['ecb']))

    def test_reference_fx_rejects_future_wrong_pair_invalid_and_expired_rates(self):
        rows = [{'date': '2026-01-03', 'base': 'GBP', 'quote': 'USD', 'rate': 1.25},
                {'date': '2026-01-04', 'base': 'USD', 'quote': 'GBP', 'rate': 0},
                {'date': '2026-01-05', 'base': 'USD', 'quote': 'GBP', 'rate': .75},
                {'date': '2025-12-20', 'base': 'USD', 'quote': 'GBP', 'rate': .70}, None]
        with patch('app.prices._get_json', return_value=rows):
            result = prices.batch_reference_fx([('USD', '2026-01-04')])
        self.assertEqual(result, {})
        with patch('app.prices._get_json') as get:
            self.assertEqual(prices.batch_reference_fx([('USD', '2999-01-01'), ('USD', 'not-date')]), {})
        get.assert_not_called()

    def test_database_migration_preserves_legacy_quote_but_requires_new_identity_check(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy.db'
            conn = sqlite3.connect(path)
            conn.execute('CREATE TABLE prices(isin TEXT PRIMARY KEY,symbol TEXT,currency TEXT,price_native REAL,price_gbp REAL,updated TEXT)')
            conn.execute("INSERT INTO prices VALUES('IE00BFMXXD54','VUAA.L','USD',100,80,'2026-09-15T10:00:00+00:00')")
            conn.commit()
            conn.close()
            for _ in range(2):
                conn = init_db(path)
                row = dict(conn.execute('SELECT * FROM prices').fetchone())
                self.assertEqual((row['symbol'], row['price_gbp'], row['identity_verified']), ('VUAA.L', 80, 0))
                self.assertIsNone(row['quote_asof'])
                self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                conn.close()


if __name__ == '__main__':
    unittest.main()

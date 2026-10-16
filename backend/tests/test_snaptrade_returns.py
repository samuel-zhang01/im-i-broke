"""Optional brokerage-return summaries: units, provenance, isolation and cache."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app import snaptrade_models as models
from app import snaptrade_store as store
from app.snaptrade_client import call, fetch_portfolio, fetch_section, restrict_transport


class ProviderFailure(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__('PRIVATE_PROVIDER_RESPONSE')


def fixture(at='2026-09-18T12:00:00+00:00'):
    account = models.account({'id': 'synthetic', 'name': 'Example',
                              'balance': {'total': {'amount': 100, 'currency': 'GBP'}}})
    return {'configured': True, 'retrieved_at': at, 'error': None,
            'accounts': [account], 'connections': models.section()}


class SnapTradeReturnTests(unittest.TestCase):
    def test_percent_units_and_unknown_method_are_preserved(self):
        result = models.return_rates({'data': [
            {'timeframe': '1M', 'return_percent': '5.97', 'created_date': '2026-09-17T22:00:00Z',
             'raw_data': 'PRIVATE', 'method': 'TWR'},
            {'timeframe': '1D', 'return_percent': 0},
            {'timeframe': 'ALL', 'return_percent': '-12.5'},
        ]})
        self.assertEqual(result['status'], 'ok')
        by_period = {row['timeframe']: row for row in result['data']}
        self.assertEqual(by_period['1M']['return_percent'], 5.97)
        self.assertEqual(by_period['1D']['return_percent'], 0)
        self.assertEqual(by_period['ALL']['return_percent'], -12.5)
        self.assertEqual(by_period['1M']['as_of'], '2026-09-17T22:00:00Z')
        self.assertIsNone(by_period['1D']['as_of'])
        self.assertTrue(all(row['method'] == 'unknown' and row['source'] == 'broker_reported'
                            for row in result['data']))
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_invalid_and_conflicting_periods_do_not_produce_fabricated_returns(self):
        result = models.return_rates({'data': [
            {'timeframe': '1D', 'return_percent': 2},
            {'timeframe': '1D', 'return_percent': 3},
            {'timeframe': '1W', 'return_percent': 0},
            {'timeframe': '1W', 'return_percent': 0},
            {'timeframe': '1M', 'return_percent': 'NaN'},
            {'timeframe': 'YTD', 'return_percent': True},
            {'timeframe': '5Y', 'return_percent': 40},
        ]})
        self.assertEqual(result['status'], 'error')
        self.assertEqual([(row['timeframe'], row['return_percent']) for row in result['data']], [('1W', 0)])
        bounded = models.return_rates({'data': [{'timeframe': '1D', 'return_percent': 1}] * 61})
        self.assertTrue(bounded['truncated'])
        self.assertEqual(len(bounded['data']), 1)

    def test_optional_unsupported_is_quiet_and_other_errors_are_sanitized(self):
        for status in (403, 404, 405, 501):
            result = fetch_section('return_rates', 'synthetic', Mock(side_effect=ProviderFailure(status)))
            self.assertEqual(result['status'], 'unsupported')
            self.assertIsNone(result['error'])
            self.assertEqual(result['data'], [])
        for status in (401, 429, 503):
            result = fetch_section('return_rates', 'synthetic', Mock(side_effect=ProviderFailure(status)))
            self.assertEqual(result['status'], 'error')
            self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(fetch_section('return_rates', 'a', Mock(return_value={'data': []}))['status'], 'empty')
        self.assertEqual(fetch_section('return_rates', 'a', Mock(return_value={}))['status'], 'error')

    def test_one_optional_request_per_account_never_combines_returns(self):
        requests = []
        def read(kind, **kwargs):
            requests.append((kind, kwargs))
            if kind == 'accounts':
                return [{'id': identifier, 'brokerage_authorization': 'link', 'is_paper': False}
                        for identifier in ('one', 'two')]
            if kind == 'account_details':
                return {'id': kwargs['account_id'], 'brokerage_authorization': 'link', 'is_paper': False,
                        'balance': {'total': None}}
            if kind == 'connections': return []
            if kind == 'balances': return []
            if kind == 'positions': return {'results': []}
            if kind == 'orders': return {'orders': []}
            if kind == 'activities': return {'data': [], 'pagination': {'total': 0}}
            if kind == 'history': return {'currency': 'GBP', 'history': []}
            if kind == 'return_rates':
                if kwargs['account_id'] == 'two': raise ProviderFailure(501)
                return {'data': [{'timeframe': '1M', 'return_percent': 5.97}]}
            raise AssertionError('Unexpected operation')
        with patch.dict(os.environ, {'SNAPTRADE_ID': 'synthetic', 'SNAPTRADE_KEY': 'synthetic'}):
            result = fetch_portfolio(read)
        self.assertIsNone(result['error'])
        self.assertNotIn('return_rates', result)
        self.assertEqual(result['accounts'][0]['return_rates']['data'][0]['return_percent'], 5.97)
        self.assertEqual(result['accounts'][1]['return_rates']['status'], 'unsupported')
        self.assertEqual(sorted(kwargs['account_id'] for kind, kwargs in requests if kind == 'return_rates'), ['one', 'two'])
        self.assertTrue(all(set(kwargs) == {'account_id'} for kind, kwargs in requests if kind == 'return_rates'))

    def test_official_sdk_and_get_only_transport_allowlist(self):
        client = Mock()
        expected = {'data': [{'timeframe': '1D', 'return_percent': -1}]}
        client.account_information.get_user_account_return_rates.return_value.body = expected
        with patch('app.snaptrade_client.create_client', return_value=client):
            self.assertEqual(call('return_rates', account_id='synthetic'), expected)
        client.account_information.get_user_account_return_rates.assert_called_once_with(account_id='synthetic')
        client.account_information.api_client.rest_client.pool_manager.clear.assert_called_once()
        request = Mock()
        guarded = restrict_transport(request)
        guarded('GET', 'https://api.snaptrade.com/accounts/synthetic/returnRates')
        self.assertFalse(request.call_args.kwargs['redirect'])
        for method, url in [
            ('POST', 'https://api.snaptrade.com/accounts/synthetic/returnRates'),
            ('GET', 'https://other.example/accounts/synthetic/returnRates'),
            ('GET', 'https://api.snaptrade.com/authorizations/synthetic/returnRates'),
        ]:
            with self.assertRaises(ValueError): guarded(method, url)

    def test_normalized_cache_persists_percentages_and_ignores_private_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'finance.db'
            result = fixture()
            result['accounts'][0]['return_rates'] = models.return_rates({'data': [
                {'timeframe': '1M', 'return_percent': 5.97, 'created_date': '2026-09-17T22:00:00Z'},
            ]})
            row = result['accounts'][0]['return_rates']['data'][0]
            row.update(raw_data='PRIVATE', method='TWR', source='PRIVATE')
            store.save_portfolio(result, path)
            loaded = store.load_portfolio(path)
            saved = loaded['accounts'][0]['return_rates']['data'][0]
            self.assertEqual(saved['return_percent'], 5.97)
            self.assertEqual(saved['method'], 'unknown')
            self.assertEqual(saved['source'], 'broker_reported')
            self.assertNotIn('PRIVATE', json.dumps(loaded))

    def test_optional_stale_return_does_not_mark_current_balances_stale(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'finance.db'
            first = fixture()
            first['accounts'][0]['return_rates'] = models.return_rates({'data': [
                {'timeframe': '1M', 'return_percent': 5.97, 'created_date': '2026-09-17T22:00:00Z'},
            ]})
            store.save_portfolio(first, path)
            second = fixture('2026-09-19T12:00:00+00:00')
            second['accounts'][0]['return_rates'] = models.section(error='Optional returns unavailable.')
            saved = store.save_portfolio(second, path)
            rates = saved['accounts'][0]['return_rates']
            self.assertTrue(rates['stale'])
            self.assertEqual(rates['cached_at'], first['retrieved_at'])
            self.assertEqual(rates['data'][0]['as_of'], '2026-09-17T22:00:00Z')
            self.assertFalse(saved['cache']['stale'])

    def test_partial_return_response_preserves_valid_updates_and_withholds_conflicts(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'finance.db'
            first = fixture()
            first['accounts'][0]['return_rates'] = models.return_rates({'data': [
                {'timeframe': '1M', 'return_percent': 5.97},
                {'timeframe': '1D', 'return_percent': 1},
            ]})
            store.save_portfolio(first, path)
            second = fixture('2026-09-19T12:00:00+00:00')
            second['accounts'][0]['return_rates'] = models.return_rates({'data': [
                {'timeframe': '1M', 'return_percent': 6.25, 'created_date': '2026-09-19'},
                {'timeframe': '1D', 'return_percent': 2},
                {'timeframe': '1D', 'return_percent': 3},
            ]})
            saved = store.save_portfolio(second, path)
            rates = saved['accounts'][0]['return_rates']
            self.assertEqual(rates['status'], 'error')
            self.assertFalse(rates['stale'])
            self.assertEqual(rates['cached_at'], second['retrieved_at'])
            self.assertEqual([(row['timeframe'], row['return_percent']) for row in rates['data']],
                             [('1M', 6.25)])
            self.assertFalse(saved['cache']['stale'])

    def test_preexisting_cache_without_optional_section_remains_readable(self):
        result = fixture()
        del result['accounts'][0]['return_rates']
        cleaned = store.clean_portfolio(result)
        self.assertEqual(cleaned['accounts'][0]['return_rates']['status'], 'empty')
        self.assertEqual(cleaned['accounts'][0]['return_rates']['data'], [])


if __name__ == '__main__':
    unittest.main()

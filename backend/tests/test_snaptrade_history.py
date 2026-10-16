"""Historical value provenance and optional endpoint failures; synthetic data."""
from datetime import date, timedelta
import json
import os
import unittest
from unittest.mock import Mock, patch

from app import snaptrade_models as models
from app.snaptrade_client import call, fetch_portfolio, fetch_section, restrict_transport


class ProviderFailure(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__('PRIVATE_RESPONSE_MUST_NOT_ESCAPE')


class SnapTradeHistoryTests(unittest.TestCase):
    def test_top_level_currency_sorted_unique_values_and_provenance(self):
        result = models.history({'currency': 'SGD', 'history': [
            {'date': date(2026, 1, 3), 'total_value': '-5'},
            {'date': '2026-01-01T00:00:00Z', 'total_value': '0', 'currency': 'USD', 'raw_data': 'PRIVATE'},
            {'date': '2026-01-03', 'total_value': '-5'},
            {'date': '2026-01-02', 'total_value': '10.25'},
        ]}, today=date(2026, 1, 3))
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['data'], [
            {'date': '2026-01-01', 'total_value': 0, 'currency': 'SGD', 'source': 'snaptrade_estimate'},
            {'date': '2026-01-02', 'total_value': 10.25, 'currency': 'SGD', 'source': 'snaptrade_estimate'},
            {'date': '2026-01-03', 'total_value': -5, 'currency': 'SGD', 'source': 'snaptrade_estimate'},
        ])
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_invalid_future_nonfinite_and_conflicting_values_are_withheld(self):
        result = models.history({'currency': 'USD', 'history': [
            {'date': '2026-01-01', 'total_value': '12'},
            {'date': '2026-01-01', 'total_value': '13'},
            {'date': '2026-01-02', 'total_value': '20'},
            {'date': '2026-01-04', 'total_value': '30'},
            {'date': 'not-a-date', 'total_value': '40'},
            {'date': '2026-01-03', 'total_value': 'NaN'},
            {'date': '2026-01-03', 'total_value': True},
            {'date': '2026-01-03', 'total_value': None},
        ]}, today=date(2026, 1, 3))
        self.assertEqual(result['status'], 'error')
        self.assertEqual([row['date'] for row in result['data']], ['2026-01-02'])
        self.assertIn('omitted', result['error'])

    def test_missing_or_ambiguous_currency_cannot_create_a_series(self):
        for code in (None, 'GBp', 'usd', '', True):
            result = fetch_section('history', 'account', Mock(return_value={
                'currency': code, 'history': [{'date': '2026-01-01', 'total_value': '100'}]}))
            self.assertEqual(result['status'], 'error')
            self.assertEqual(result['data'], [])

    def test_history_is_bounded_to_latest_400_observations(self):
        start = date(2020, 1, 1)
        result = models.history({'currency': 'GBP', 'history': [
            {'date': (start + timedelta(days=i)).isoformat(), 'total_value': str(i)}
            for i in reversed(range(500))]}, today=date(2022, 1, 1))
        self.assertEqual(len(result['data']), 400)
        self.assertEqual(result['data'][0]['total_value'], 100)
        self.assertEqual(result['data'][-1]['total_value'], 499)
        self.assertTrue(result['truncated'])

    def test_optional_addon_disabled_is_not_empty_or_a_credential_leak(self):
        result = fetch_section('history', 'account', Mock(side_effect=ProviderFailure(403)))
        self.assertEqual(result['status'], 'unsupported')
        self.assertIn('add-on', result['error'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(fetch_section('history', 'a', Mock(return_value={'currency': 'GBP', 'history': []}))['status'], 'empty')

    def test_history_failure_does_not_fail_balances_and_is_read_once_per_account(self):
        reads = []
        def read(kind, **kwargs):
            reads.append((kind, kwargs))
            if kind == 'accounts':
                return [{'id': 'a', 'brokerage_authorization': 'link', 'is_paper': False,
                         'balance': {'total': {'amount': '100', 'currency': 'GBP'}}}]
            if kind == 'account_details':
                return {'id': 'a', 'brokerage_authorization': 'link', 'is_paper': False,
                        'balance': {'total': {'amount': '105', 'currency': 'GBP'}}}
            if kind == 'connections': return []
            if kind == 'balances': return [{'cash': '20', 'currency': {'code': 'GBP'}}]
            if kind == 'positions': return {'results': []}
            if kind == 'orders': return {'orders': []}
            if kind == 'activities': return {'data': [], 'pagination': {'total': 0}}
            if kind == 'history': raise ProviderFailure(403)
            raise AssertionError('Unknown operation')
        with patch.dict(os.environ, {'SNAPTRADE_ID': 'synthetic', 'SNAPTRADE_KEY': 'synthetic'}):
            result = fetch_portfolio(read)
        account = result['accounts'][0]
        self.assertIsNone(result['error'])
        self.assertEqual(account['total_value'], 105)
        self.assertEqual(account['balances']['data'][0]['cash'], 20)
        self.assertEqual(account['history']['status'], 'unsupported')
        self.assertEqual([kwargs for kind, kwargs in reads if kind == 'history'], [{'account_id': 'a'}])

    def test_official_sdk_history_operation_and_read_only_transport(self):
        client = Mock()
        client.account_information.get_account_balance_history.return_value.body = {'history': [], 'currency': 'GBP'}
        with patch('app.snaptrade_client.create_client', return_value=client):
            self.assertEqual(call('history', account_id='a'), {'history': [], 'currency': 'GBP'})
        client.account_information.get_account_balance_history.assert_called_once_with(account_id='a')
        client.account_information.api_client.rest_client.pool_manager.clear.assert_called_once()
        request = Mock()
        guarded = restrict_transport(request)
        guarded('GET', 'https://api.snaptrade.com/accounts/a/balanceHistory')
        self.assertFalse(request.call_args.kwargs['redirect'])
        with self.assertRaises(ValueError):
            guarded('POST', 'https://api.snaptrade.com/accounts/a/balanceHistory')

    def test_actual_mutualfund_discriminator_keeps_cash_equivalent_separate(self):
        value = models.position({'instrument': {'kind': 'mutualfund'},
                                 'units': '2', 'price': '10', 'cost_basis': '8',
                                 'currency': 'USD', 'cash_equivalent': True})
        self.assertEqual(value['value'], 20)
        self.assertEqual(value['unrealized_pnl'], 4)
        self.assertTrue(value['is_cash'])


if __name__ == '__main__':
    unittest.main()

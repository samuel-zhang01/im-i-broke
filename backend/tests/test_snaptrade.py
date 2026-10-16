"""Financial normalization and provider failure tests use synthetic values only."""
import json
import os
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib3.exceptions import ReadTimeoutError

from app import snaptrade_models as model
from app.snaptrade_client import (fetch_portfolio, fetch_section, restrict_transport, create_client,
                                 distinct_accounts, safe_error, RequestBudgetExceeded)


class ProviderFailure(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__('PRIVATE_KEY_AND_FINANCIAL_RESPONSE_MUST_NOT_ESCAPE')


class SnapTradeTests(unittest.TestCase):
    def test_account_minimal_projection_and_missing_values(self):
        account = model.account({'id': 'a', 'name': 'ISA', 'number': '1234-5678-9876',
                                 'balance': {'total': {'amount': '0', 'currency': 'GBP'}},
                                 'raw_data': {'credential': 'PRIVATE'}, 'institution_account_id': 'PRIVATE',
                                 'sync_status': {'holdings': {'initial_sync_completed': False}}})
        self.assertEqual(account['number_suffix'], '9876')
        self.assertEqual(account['total_value'], 0)
        self.assertEqual(account['currency'], 'GBP')
        self.assertFalse(account['initial_sync_complete'])
        self.assertNotIn('PRIVATE', json.dumps(account))
        self.assertIsNone(model.account({})['total_value'])
        for value in (None, float('inf'), 'NaN', True, 'not a number'):
            self.assertIsNone(model.number(value))
        self.assertIsNone(model.currency('GBp'))

    def test_position_currency_fractional_short_and_option_multiplier(self):
        base = {'instrument': {'kind': 'stock', 'id': 's', 'symbol': 'ABC', 'currency': 'USD'},
                'units': '1.5', 'price': '40', 'cost_basis': '30', 'currency': 'GBP'}
        position = model.position(base)
        self.assertEqual(position['value'], 60)
        self.assertEqual(position['unrealized_pnl'], 15)
        self.assertEqual(position['currency'], 'GBP')
        self.assertEqual(model.position({**base, 'units': '-2'})['unrealized_pnl'], -20)
        option = {**base, 'instrument': {'kind': 'option', 'multiplier': '10'}, 'units': '2', 'price': '3', 'cost_basis': '1'}
        self.assertEqual(model.position(option)['value'], 60)
        self.assertEqual(model.position(option)['unrealized_pnl'], 40)
        self.assertIsNone(model.position({**option, 'instrument': {'kind': 'option'}})['value'])
        self.assertIsNone(model.position({**base, 'instrument': {'kind': 'bond'}})['value'])
        self.assertIsNone(model.position({**base, 'currency': None})['currency'])
        self.assertIsNone(model.position({**base, 'price': None})['value'])
        self.assertTrue(model.position({**base, 'cash_equivalent': True})['is_cash'])

    def test_current_account_total_does_not_inherit_daily_sync_or_positions_time(self):
        total_time = '2026-09-17T18:00:00Z'
        position_time = '2026-09-18T14:00:00Z'

        def read(kind, **kwargs):
            if kind == 'accounts':
                return [{'id': 'a', 'brokerage_authorization': 'link', 'is_paper': False,
                         'balance': {'total': {'amount': 100, 'currency': 'GBP'}},
                         'sync_status': {'holdings': {'last_successful_sync': total_time}}}]
            if kind == 'account_details':
                return {'id': 'a', 'brokerage_authorization': 'link', 'is_paper': False,
                        'balance': {'total': {'amount': 110, 'currency': 'GBP'}},
                        'sync_status': {'holdings': {'last_successful_sync': total_time}}}
            if kind == 'connections': return []
            if kind == 'positions': return {'results': [], 'data_freshness': {'as_of': position_time}}
            if kind == 'balances': return [{'cash': 100, 'currency': {'code': 'GBP'}}]
            if kind == 'orders': return {'orders': []}
            if kind == 'activities': return {'data': [], 'pagination': {'total': 0}}
            if kind == 'history': return {'currency': 'GBP', 'history': []}
            if kind == 'return_rates': return []
            raise AssertionError(kind)

        with patch.dict(os.environ, {'SNAPTRADE_ID': 'synthetic', 'SNAPTRADE_KEY': 'synthetic'}):
            result = fetch_portfolio(read)
        account = result['accounts'][0]
        self.assertEqual(account['total_value'], 110)
        self.assertIsNone(account['total_value_as_of'])
        self.assertEqual(account['holdings_synced_at'], position_time)
        self.assertEqual(account['positions']['as_of'], position_time)

    def test_cash_buying_power_and_activity_signs_are_distinct(self):
        row = model.balance({'currency': {'code': 'SGD'}, 'cash': '-20', 'buying_power': '100'})
        self.assertEqual(row, {'currency': 'SGD', 'cash': -20, 'buying_power': 100})
        activity = model.activity({'amount': '-100', 'units': '2', 'type': 'BUY', 'currency': {'code': 'USD'}})
        self.assertEqual(activity['amount'], -100)
        self.assertEqual(activity['quantity'], 2)
        self.assertEqual(activity['type'], 'BUY')
        order = model.order({'total_quantity': '4', 'filled_quantity': '0', 'status': 'CANCELED'})
        self.assertEqual(order['filled_quantity'], 0)
        self.assertIsNone(order['execution_price'])

    def test_sterling_pence_position_prices_and_cost_convert_exactly_once(self):
        base = {'instrument': {'kind': 'stock', 'id': 's', 'symbol': 'TEST.L',
                               'currency': 'GBX', 'exchange': 'XLON'},
                'units': '2', 'price': '12345', 'cost_basis': '10000', 'currency': 'GBX'}
        for pence_unit in ('GBX', 'GBp'):
            position = model.position({**base, 'currency': pence_unit})
            self.assertEqual(position['quantity'], 2)
            self.assertEqual(position['price'], 123.45)
            self.assertEqual(position['average_cost'], 100)
            self.assertEqual(position['currency'], 'GBP')
            self.assertEqual(position['listing_currency'], 'GBP')
            self.assertAlmostEqual(position['value'], 246.9)
            self.assertAlmostEqual(position['unrealized_pnl'], 46.9)
        already_pounds = model.position({**base, 'currency': 'GBP', 'price': '123.45', 'cost_basis': '100'})
        self.assertAlmostEqual(already_pounds['value'], 246.9)
        other_currency = model.position({**base, 'currency': 'USD', 'price': '200', 'cost_basis': '150'})
        self.assertEqual(other_currency['value'], 400)
        self.assertEqual(other_currency['currency'], 'USD')
        missing = model.position({**base, 'price': None, 'cost_basis': None})
        self.assertIsNone(missing['value'])
        self.assertIsNone(missing['unrealized_pnl'])
        self.assertIsNone(model.currency('GBp'))  # Generic currency parsing remains strict.

    def test_independent_sections_survive_failures_and_holdings_unavailable(self):
        active = 0
        peak = 0
        lock = threading.Lock()
        calls = []
        def read(kind, **kwargs):
            nonlocal active, peak
            with lock:
                calls.append((kind, kwargs)); active += 1; peak = max(peak, active)
            try:
                time.sleep(.005)
                if kind == 'accounts':
                    return [{'id': 'a', 'institution_name': 'Broker', 'brokerage_authorization': 'c', 'is_paper': False},
                            {'id': 'b', 'sync_status': {'holdings': {'holdings_unavailable': True}}}]
                if kind == 'account_details':
                    return {'id': kwargs['account_id'], 'brokerage_authorization': 'c', 'is_paper': False,
                            'balance': {'total': None}}
                if kind == 'connections':
                    return [{'id': 'c', 'type': 'trade', 'disabled': True}]
                if kind == 'positions': raise ProviderFailure(503)
                if kind == 'orders': raise ProviderFailure(404)
                if kind == 'balances': return [{'cash': 15, 'currency': {'code': 'USD'}}]
                return {'data': [{'id': 'act', 'amount': 1}], 'pagination': {'total': 1}}
            finally:
                with lock: active -= 1
        with patch.dict(os.environ, {'SNAPTRADE_ID': 'synthetic', 'SNAPTRADE_KEY': 'synthetic'}):
            data = fetch_portfolio(read)
        self.assertEqual(len(data['accounts']), 2)
        self.assertLessEqual(peak, 4)
        a, b = data['accounts']
        self.assertEqual(a['positions']['status'], 'error')
        self.assertEqual(a['orders']['status'], 'unsupported')
        self.assertEqual(a['balances']['data'][0]['cash'], 15)
        self.assertEqual(a['activities']['status'], 'ok')
        self.assertEqual(b['balances']['status'], 'unsupported')
        self.assertFalse(any(k in ('balances', 'positions') and kw['account_id'] == 'b' for k, kw in calls))
        self.assertNotIn('PRIVATE', json.dumps(data))
        self.assertFalse(any('user_id' in kw or 'user_secret' in kw for _, kw in calls))
        self.assertEqual(data['connections']['data'][0]['is_read_only'], False)

    def test_bounded_activity_pagination_and_duplicate_ids(self):
        pages = [
            {'data': [{'id': 'one'}, {'id': 'two'}], 'pagination': {'total': 400}},
            {'data': [{'id': 'two'}, {'id': 'three'}], 'pagination': {'total': 400}},
        ]
        read = Mock(side_effect=pages)
        data = fetch_section('activities', 'a', read)
        self.assertEqual([r['id'] for r in data['data']], ['one', 'two', 'three'])
        self.assertTrue(data['truncated'])
        self.assertEqual(read.call_count, 2)
        self.assertEqual(read.call_args.kwargs['offset'], 100)
        self.assertEqual(read.call_args.kwargs['limit'], 100)

    def test_malformed_payload_and_provider_failure_are_not_empty_success(self):
        for failure in (ProviderFailure(401), ProviderFailure(429), ValueError('PRIVATE')):
            result = fetch_section('positions', 'a', Mock(side_effect=failure))
            self.assertEqual(result['status'], 'error')
            self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(fetch_section('positions', 'a', Mock(return_value={}))['status'], 'error')
        self.assertEqual(fetch_section('positions', 'a', Mock(return_value={'results': []}))['status'], 'empty')

    def test_later_activity_page_failure_retains_successful_page(self):
        read = Mock(side_effect=[{'data': [{'id': 'one', 'amount': 4}], 'pagination': {'total': 300}}, ProviderFailure(429)])
        result = fetch_section('activities', 'a', read)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['data'][0]['amount'], 4)
        self.assertTrue(result['truncated'])
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_confirmed_duplicate_links_prefer_usable_connection_not_masked_suffix(self):
        rows = [
            {'id': 'a', 'institution_name': 'Broker', 'institution_account_id': 'same', 'brokerage_authorization': 'disabled'},
            {'id': 'b', 'institution_name': 'Broker', 'institution_account_id': 'same', 'brokerage_authorization': 'enabled'},
            {'id': 'c', 'institution_name': 'Other Broker', 'institution_account_id': 'same'},
            {'id': 'd', 'institution_name': 'Broker', 'number': '***1234'},
            {'id': 'e', 'institution_name': 'Broker', 'number': '***1234'},
        ]
        selected, duplicates = distinct_accounts(rows, [{'id': 'disabled', 'disabled': True}])
        self.assertEqual(duplicates, 1)
        self.assertEqual([r['id'] for r in selected], ['b', 'c', 'd', 'e'])

    def test_duplicate_link_identity_keeps_live_and_paper_namespaces_separate(self):
        base = {'institution_name': 'Broker', 'institution_account_id': 'same'}
        rows = [
            {**base, 'id': 'live', 'is_paper': False},
            {**base, 'id': 'paper-old', 'is_paper': True, 'brokerage_authorization': 'disabled'},
            {**base, 'id': 'paper-current', 'is_paper': True, 'brokerage_authorization': 'enabled'},
        ]
        selected, duplicates = distinct_accounts(rows, [{'id': 'disabled', 'disabled': True}])
        self.assertEqual(duplicates, 1)
        self.assertEqual([r['id'] for r in selected], ['live', 'paper-current'])
        self.assertEqual([model.account(r)['is_paper'] for r in selected], [False, True])

    def test_readonly_transport_refuses_write_redirect_or_other_origin(self):
        request = Mock()
        restricted = restrict_transport(request)
        restricted('GET', 'https://api.snaptrade.com/api/v1/accounts?clientId=synthetic')
        self.assertFalse(request.call_args.kwargs['redirect'])
        self.assertFalse(request.call_args.kwargs['retries'])
        for method, url in [('POST', 'https://api.snaptrade.com/api/v1/accounts'),
                            ('GET', 'https://other.example/api/v1/accounts'),
                            ('GET', 'http://api.snaptrade.com/api/v1/accounts'),
                            ('GET', 'https://api.snaptrade.com/api/v1/trade'),
                            ('GET', 'https://api.snaptrade.com:8443/api/v1/accounts'),
                            ('GET', 'https://api.snaptrade.com/api/v1/accounts/a/../../trade')]:
            with self.assertRaises(ValueError): restricted(method, url)
        self.assertEqual(request.call_count, 1)

    def test_only_live_positions_and_totals_get_longer_bounded_transport_timeout(self):
        request = Mock()
        restricted = restrict_transport(request)
        for path, read_timeout, total_timeout in (
            ('accounts', 12, 15), ('accounts/a/balances', 12, 15),
            ('accounts/a/activities', 12, 15), ('accounts/a/positions/all', 25, 28),
            ('accounts/a/positions/all/', 25, 28),
            ('accounts/a/balanceHistory', 12, 15), ('accounts/a/returnRates', 12, 15),
            ('accounts/12345678-1234-1234-1234-123456789abc', 25, 28),
        ):
            restricted('GET', 'https://api.snaptrade.com/api/v1/' + path)
            options = request.call_args.kwargs
            self.assertEqual(options['timeout'].connect_timeout, 3)
            self.assertEqual(options['timeout'].read_timeout, read_timeout)
            self.assertEqual(options['timeout'].total, total_timeout)
            self.assertFalse(options['redirect'])
            self.assertFalse(options['retries'])

    def test_provider_failures_have_actionable_categories_without_private_details(self):
        secret = 'PRIVATE_SIGNED_URL_AND_PROVIDER_BODY'
        for error, expected in (
            (ReadTimeoutError(None, 'https://example.test/' + secret, secret), 'timed out'),
            (TimeoutError(secret), 'timed out'),
            (RequestBudgetExceeded(secret), 'time limit'),
            (ProviderFailure(503), 'temporary server error'),
            (ProviderFailure(429), 'limiting requests'),
            (ValueError(secret), 'invalid or inconsistent response'),
            (KeyError(secret), 'invalid or inconsistent response'),
        ):
            with self.subTest(error=type(error).__name__, expected=expected):
                message = safe_error(error)
                self.assertIn(expected, message)
                self.assertNotIn('PRIVATE', message)
                section = fetch_section('positions', 'a', Mock(side_effect=error))
                self.assertEqual(section['status'], 'error')
                self.assertIn(expected, section['error'])
                self.assertNotIn('PRIVATE', json.dumps(section))

    def test_personal_sdk_auth_and_no_network_at_construction(self):
        with patch.dict(os.environ, {'SNAPTRADE_ID': 'synthetic-id', 'SNAPTRADE_KEY': 'synthetic-key'}):
            client = create_client()
        self.assertEqual(client.account_information.api_client.configuration.auth_mode, 'personalApiKey')
        self.assertIs(client.account_information.api_client, client.connections.api_client)
        client.account_information.api_client.rest_client.pool_manager.clear()

    def test_missing_configuration_makes_no_provider_calls(self):
        with patch.dict(os.environ, {'SNAPTRADE_ID': '', 'SNAPTRADE_KEY': ''}):
            read = Mock()
            result = fetch_portfolio(read)
        self.assertFalse(result['configured'])
        read.assert_not_called()

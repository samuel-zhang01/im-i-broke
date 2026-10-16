"""Current account totals use bounded read-only detail requests; no live data."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from app import snaptrade_client as client
from app import snaptrade_models as models
from app import snaptrade_routes as routes
from app import snaptrade_store as store


ACCOUNT_ID = '12345678-1234-1234-1234-123456789abc'
DAILY_TIME = '2026-09-18T09:00:00Z'
POSITION_TIME = '2026-09-18T17:00:00Z'


def discovered(identifier=ACCOUNT_ID, **overrides):
    return {'id': identifier, 'brokerage_authorization': 'connection', 'is_paper': False,
            'name': 'Original account', 'institution_name': 'Example broker', 'raw_type': 'margin',
            'balance': {'total': {'amount': 100, 'currency': 'USD'}},
            'sync_status': {'holdings': {'last_successful_sync': DAILY_TIME}}, **overrides}


def current(identifier=ACCOUNT_ID, **overrides):
    return {'id': identifier, 'brokerage_authorization': 'connection', 'is_paper': False,
            'name': 'Different detail label', 'institution_name': 'Detail label', 'raw_type': 'other',
            'balance': {'total': {'amount': 120, 'currency': 'GBP'}},
            'sync_status': {'holdings': {'last_successful_sync': DAILY_TIME}}, **overrides}


class ProviderFailure(Exception):
    status = 503

    def __init__(self):
        super().__init__('PRIVATE_PROVIDER_RESPONSE')


class CurrentAccountDetailTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {'SNAPTRADE_ID': 'synthetic', 'SNAPTRADE_KEY': 'synthetic'})
        environment.start()
        self.addCleanup(environment.stop)
        self.calls = []
        self.accounts = [discovered()]
        self.details = {ACCOUNT_ID: current()}

    def read(self, kind, **kwargs):
        self.calls.append((kind, kwargs))
        if kind == 'accounts': return deepcopy(self.accounts)
        if kind == 'connections': return []
        if kind == 'account_details':
            result = self.details[kwargs['account_id']]
            if isinstance(result, Exception): raise result
            return deepcopy(result)
        if kind == 'balances': return [{'cash': 5, 'currency': 'GBP'}]
        if kind == 'positions': return {'results': [], 'data_freshness': {'as_of': POSITION_TIME}}
        if kind == 'orders': return {'orders': []}
        if kind == 'activities': return {'data': [], 'pagination': {'total': 0}}
        if kind == 'history': return {'currency': 'GBP', 'history': []}
        if kind == 'return_rates': return {'data': []}
        raise AssertionError(kind)

    def test_current_total_currency_and_cash_are_independent_of_discovery_and_positions(self):
        result = client.fetch_portfolio(self.read)
        self.assertIsNone(result['error'])
        account = result['accounts'][0]
        self.assertEqual((account['total_value'], account['currency']), (120, 'GBP'))
        self.assertIsNone(account['total_value_as_of'])
        self.assertEqual(account['holdings_synced_at'], POSITION_TIME)
        self.assertEqual(account['positions']['as_of'], POSITION_TIME)
        self.assertEqual(account['balances']['data'], [{'currency': 'GBP', 'cash': 5, 'buying_power': None}])
        self.assertEqual((account['name'], account['institution'], account['type']),
                         ('Original account', 'Example broker', 'margin'))
        self.assertEqual([kwargs for kind, kwargs in self.calls if kind == 'account_details'],
                         [{'account_id': ACCOUNT_ID}])

    def test_explicit_null_detail_total_is_unknown_and_zero_remains_zero(self):
        for balance, currency in (({'total': None}, None),
                                  ({'total': {'amount': None, 'currency': 'GBP'}}, 'GBP')):
            with self.subTest(balance=balance):
                self.details[ACCOUNT_ID] = current(balance=balance)
                result = client.fetch_portfolio(self.read)
                self.assertIsNone(result['error'])
                account = result['accounts'][0]
                self.assertIsNone(account['total_value'])
                self.assertEqual(account['currency'], currency)
                self.assertIsNone(account['total_value_as_of'])
        self.details[ACCOUNT_ID] = current(balance={'total': {'amount': 0, 'currency': 'GBP'}})
        self.assertEqual(client.fetch_portfolio(self.read)['accounts'][0]['total_value'], 0)

    def test_unknown_unavailable_account_skips_detail_without_turning_into_zero(self):
        self.accounts.append(discovered('unavailable', balance={'total': None},
            sync_status={'holdings': {'holdings_unavailable': True}}))
        result = client.fetch_portfolio(self.read)
        self.assertIsNone(result['error'])
        account = result['accounts'][1]
        self.assertIsNone(account['total_value'])
        self.assertIsNone(account['currency'])
        self.assertEqual(account['positions']['status'], 'unsupported')
        self.assertFalse(any(kind in {'account_details', 'positions', 'balances'} and
                             kwargs['account_id'] == 'unavailable' for kind, kwargs in self.calls))

    def test_identity_mismatch_or_invalid_amount_aborts_before_independent_sections(self):
        cases = [current(id='wrong'), current(brokerage_authorization='wrong'),
                 current(is_paper=True), current(is_paper=None),
                 current(balance=None), current(balance={}), current(balance=[]),
                 current(balance={'total': []}), current(balance={'total': {'currency': 'GBP'}}),
                 current(balance={'total': {'amount': 'NaN', 'currency': 'GBP'}}),
                 current(balance={'total': {'amount': 1, 'currency': 'GBp'}})]
        for detail in cases:
            with self.subTest(detail=detail):
                self.calls.clear()
                self.details[ACCOUNT_ID] = detail
                result = client.fetch_portfolio(self.read)
                self.assertEqual(result['accounts'], [])
                self.assertIn('Current account values could not be retrieved', result['error'])
                self.assertIsNotNone(models.timestamp(result['retrieved_at']))
                self.assertTrue(all(kind in {'accounts', 'connections', 'account_details'}
                                    for kind, _ in self.calls))
                self.assertNotIn('PRIVATE', json.dumps(result))

    def test_detail_failure_retains_newer_cached_total_and_adds_no_snapshot(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(store.db, 'DB_PATH', Path(folder) / 'test.db'):
            saved = store.save_portfolio(client.fetch_portfolio(self.read))
            self.details[ACCOUNT_ID] = ProviderFailure()
            failed = client.fetch_portfolio(self.read)
            self.assertEqual(failed['accounts'], [])
            with self.assertRaises(ValueError): store.save_portfolio(failed)
            with patch.object(routes, 'configured', return_value=True), patch.object(routes, '_next_read', 0), \
                    patch.object(routes, 'fetch_portfolio', return_value=failed):
                response = routes.refresh_portfolio()
            self.assertEqual(response.status_code, 200)
            retained = json.loads(response.body)
            self.assertEqual(retained['accounts'][0]['total_value'], 120)
            self.assertEqual(retained['retrieved_at'], saved['retrieved_at'])
            self.assertTrue(retained['cache']['stale'])
            self.assertIn('Current account values', retained['cache']['refresh_error'])
            self.assertNotIn('PRIVATE', json.dumps(retained))
            self.assertEqual(len(store.list_snapshots()), 1)

    def test_details_cannot_start_after_global_request_budget_expires(self):
        started = threading.Barrier(2)
        clock = {'expired': False}

        def root_read(kind, **kwargs):
            started.wait(timeout=2)
            result = self.read(kind, **kwargs)
            clock['expired'] = True
            return result

        with patch.object(client.time, 'monotonic', side_effect=lambda: 1000 if clock['expired'] else 100):
            result = client.fetch_portfolio(root_read)
        self.assertEqual(result['accounts'], [])
        self.assertIn('Current account values', result['error'])
        self.assertEqual(sorted(kind for kind, _ in self.calls), ['accounts', 'connections'])

    def test_longer_positions_timeout_does_not_bypass_expired_shared_start_budget(self):
        clock = {'expired': False}

        def expire_after_details(kind, **kwargs):
            result = self.read(kind, **kwargs)
            if kind == 'account_details':
                clock['expired'] = True
            return result

        with patch.object(client.time, 'monotonic', side_effect=lambda: 1000 if clock['expired'] else 100):
            result = client.fetch_portfolio(expire_after_details)
        self.assertIsNone(result['error'])
        self.assertEqual(len(result['accounts']), 1)
        positions = result['accounts'][0]['positions']
        self.assertEqual(positions['status'], 'error')
        self.assertIn('time limit', positions['error'])
        self.assertTrue(all(kind in {'accounts', 'connections', 'account_details'} for kind, _ in self.calls))

    def test_all_accounts_current_data_precedes_optional_history_budget_consumption(self):
        self.accounts = [discovered(str(index)) for index in range(4)]
        self.details = {str(index): current(str(index)) for index in range(4)}
        clock = {'expired': False}

        def expire_on_optional_feed(kind, **kwargs):
            if kind in {'orders', 'activities', 'history', 'return_rates'}:
                clock['expired'] = True
            return self.read(kind, **kwargs)

        with patch.object(client.time, 'monotonic', side_effect=lambda: 1000 if clock['expired'] else 100):
            result = client.fetch_portfolio(expire_on_optional_feed)
        self.assertIsNone(result['error'])
        self.assertEqual(len(result['accounts']), 4)
        for account in result['accounts']:
            self.assertEqual(account['positions']['status'], 'empty')
            self.assertEqual(account['positions']['as_of'], POSITION_TIME)
            self.assertEqual(account['balances']['status'], 'ok')
        self.assertEqual({(kind, kwargs['account_id']) for kind, kwargs in self.calls
                          if kind in {'positions', 'balances'}},
                         {(kind, str(index)) for kind in ('positions', 'balances') for index in range(4)})

    def test_details_share_four_worker_limit_and_each_known_account_is_read_once(self):
        self.accounts = [discovered(str(index)) for index in range(8)]
        self.details = {str(index): current(str(index)) for index in range(8)}
        lock, active, peak = threading.Lock(), 0, 0

        def counted_read(kind, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(.002)
                return self.read(kind, **kwargs)
            finally:
                with lock: active -= 1

        result = client.fetch_portfolio(counted_read)
        self.assertIsNone(result['error'])
        self.assertEqual(len(result['accounts']), 8)
        self.assertLessEqual(peak, 4)
        self.assertEqual(sorted(kwargs['account_id'] for kind, kwargs in self.calls if kind == 'account_details'),
                         [str(index) for index in range(8)])

    def test_sdk_detail_operation_and_narrow_read_only_allowlist(self):
        sdk = Mock()
        sdk.account_information.get_user_account_details.return_value.body = current()
        with patch.object(client, 'create_client', return_value=sdk):
            self.assertEqual(client.call('account_details', account_id=ACCOUNT_ID), current())
        sdk.account_information.get_user_account_details.assert_called_once_with(account_id=ACCOUNT_ID)
        sdk.account_information.api_client.rest_client.pool_manager.clear.assert_called_once()
        request = Mock()
        guarded = client.restrict_transport(request)
        for prefix in ('', '/api/v1'):
            guarded('GET', f'https://api.snaptrade.com{prefix}/accounts/{ACCOUNT_ID}')
        self.assertFalse(request.call_args.kwargs['redirect'])
        self.assertFalse(request.call_args.kwargs['retries'])
        for method, path in [('POST', f'/accounts/{ACCOUNT_ID}'), ('PUT', f'/accounts/{ACCOUNT_ID}'),
                             ('DELETE', f'/accounts/{ACCOUNT_ID}'), ('GET', '/accounts/not-an-account'),
                             ('GET', f'/accounts/{ACCOUNT_ID}/refresh'),
                             ('GET', f'/authorizations/{ACCOUNT_ID}/accounts'),
                             ('GET', f'/accounts/{ACCOUNT_ID}/../trade')]:
            with self.assertRaises(ValueError): guarded(method, 'https://api.snaptrade.com' + path)
        self.assertEqual(request.call_count, 2)


if __name__ == '__main__':
    unittest.main()

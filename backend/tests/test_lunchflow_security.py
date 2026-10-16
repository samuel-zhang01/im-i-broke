"""Independent banking security checks; all credentials and payloads are synthetic."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request

from app import lunchflow_client as provider


class LunchFlowTransportSecurityTests(unittest.TestCase):
    def setUp(self):
        self.secret = 'synthetic-lunchflow-secret-never-return'
        self.environment = patch.dict(os.environ, {'LUNCH_FLOW_API': self.secret})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def response(self, payload):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(payload).encode()
        return response

    def test_fixed_origin_key_header_no_redirects_or_inherited_proxy(self):
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://untrusted.invalid:9999'}), \
                patch.object(provider, 'build_opener') as build:
            build.return_value.open.return_value = self.response({'accounts': [], 'total': 0})
            provider.read('accounts')
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, 'https://www.lunchflow.app/api/v1/accounts')
            self.assertEqual(request.get_header('X-api-key'), self.secret)
            self.assertNotIn(self.secret, request.full_url)
            proxies = [arg for arg in build.call_args.args if isinstance(arg, ProxyHandler)]
            self.assertTrue(proxies, 'An explicit empty ProxyHandler must disable inherited proxies')
            self.assertEqual(proxies[0].proxies, {})
            self.assertTrue(any(isinstance(arg, provider.NoRedirects) for arg in build.call_args.args))
        self.assertIsNone(provider.NoRedirects().redirect_request(
            Request('https://www.lunchflow.app/api/v1/accounts'), None, 302, '', {},
            'https://attacker.invalid/collect'))

    def test_credential_and_path_validation_happen_before_network(self):
        with patch.object(provider, 'build_opener') as build:
            with patch.dict(os.environ, {'LUNCH_FLOW_API': ''}):
                with self.assertRaises(provider.ProviderError):
                    provider.read('accounts')
            for value in ['../../secrets', 'https://attacker.invalid', '1?key=x',
                          '1#fragment', '1%2f..', 'a/b', True, None, 'x' * 161]:
                with self.subTest(value=value), self.assertRaises(provider.ProviderError):
                    provider.read('transactions', value)
            with self.assertRaises(provider.ProviderError):
                provider.read('https://attacker.invalid')
            build.assert_not_called()

    def test_errors_never_echo_provider_body_url_or_key(self):
        sensitive = self.secret + ' private-account-record'
        for status in [301, 302, 401, 403, 429, 500, 503]:
            with self.subTest(status=status), patch.object(provider, 'build_opener') as build:
                build.return_value.open.side_effect = HTTPError(
                    'https://attacker.invalid/' + sensitive, status, sensitive, {},
                    io.BytesIO(json.dumps({'message': sensitive}).encode()))
                with self.assertRaises(provider.ProviderError) as result:
                    provider.read('accounts')
                self.assertNotIn(sensitive, str(result.exception))
                self.assertNotIn(self.secret, str(result.exception))
        with patch.object(provider, 'build_opener') as build:
            build.return_value.open.side_effect = URLError(sensitive)
            with self.assertRaises(provider.ProviderError) as result:
                provider.read('accounts')
            self.assertNotIn(sensitive, str(result.exception))
        self.assertNotIn(sensitive, provider.safe_error(RuntimeError(sensitive)))

    def test_oversized_and_invalid_responses_do_not_reach_storage(self):
        for payload in [b'x' * 65, b'{not-json', b'[]', b'null']:
            with self.subTest(length=len(payload)), patch.object(provider, 'MAX_BYTES', 64), \
                    patch.object(provider, 'build_opener') as build:
                response = self.response({})
                response.read.return_value = payload
                build.return_value.open.return_value = response
                with self.assertRaises(provider.ProviderError):
                    provider.read('accounts')
                response.read.assert_called_once_with(65)


class LunchFlowPayloadSecurityTests(unittest.TestCase):
    def transaction(self, **changes):
        return {'id': 'txn_1', 'accountId': 42, 'amount': -12.50, 'currency': 'GBP',
                'date': '2026-09-15', 'merchant': 'Synthetic shop',
                'description': 'Synthetic purchase', 'isPending': False, **changes}

    def test_invalid_money_is_rejected_without_its_value_in_error(self):
        for value in [True, False, None, 'nan', 'NaN', 'Infinity', '-Infinity',
                      float('nan'), float('inf'), 10 ** 20, {'amount': 1}]:
            with self.subTest(type=type(value).__name__), self.assertRaises(provider.ProviderError):
                provider.number(value)
        self.assertEqual(provider.number(0), 0)
        self.assertEqual(provider.number('-12.50'), -12.5)

    def test_transaction_account_identity_currency_and_status_are_validated(self):
        for changes in [{'accountId': 99}, {'id': 'bad\nidentifier'}, {'id': None},
                        {'id': True}, {'id': {'nested': 'identifier'}}, {'id': ['identifier']},
                        {'currency': 'GBP<script>'}, {'currency': ''},
                        {'isPending': 'false'}, {'date': '2026-02-30'},
                        {'amount': float('nan')}]:
            with self.subTest(changes=list(changes)), self.assertRaises(provider.ProviderError):
                provider.transactions({'transactions': [self.transaction(**changes)], 'total': 1}, '42')

    def test_incomplete_or_duplicate_lists_are_rejected(self):
        for payload in [
            {'transactions': [self.transaction()], 'total': 2},
            {'transactions': [self.transaction(), self.transaction()], 'total': 2},
            {'transactions': {'unexpected': []}, 'total': 0},
        ]:
            with self.assertRaises(provider.ProviderError):
                provider.transactions(payload, '42')
        for payload in [
            {'accounts': [], 'total': 1},
            {'accounts': [{'id': 42}, {'id': 42}], 'total': 2},
            {'accounts': 'unexpected', 'total': 0},
        ]:
            with self.assertRaises(provider.ProviderError):
                provider.list_accounts(payload)

    def test_missing_account_currency_is_not_fabricated_and_each_transaction_keeps_its_currency(self):
        account = provider.list_accounts({'accounts': [{'id': 42, 'name': 'Synthetic account'}], 'total': 1})[0]
        self.assertIsNone(account['currency'])
        records = provider.transactions({'transactions': [self.transaction(), self.transaction(id='txn_2', currency='SGD')], 'total': 2}, '42')
        self.assertEqual([row['currency'] for row in records], ['GBP', 'SGD'])


class LunchFlowApiSecurityTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from app import main, lunchflow_store as store
        from app.db import init_db
        self.main, self.store = main, store
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'finance.db'
        conn = init_db(self.path)
        store.ensure_schema(conn)
        conn.commit()
        conn.close()
        self.secret = 'synthetic-api-secret-not-for-browser'
        self.environment = patch.dict(os.environ, {
            'LUNCH_FLOW_API': self.secret, 'LUNCH_FLOW_SYNC_SECONDS': '0',
            'FINANCE_AUTH_ENABLED': 'true', 'FINANCE_DEPLOYMENT': 'local',
            'FINANCE_AUTH_DB_PATH': str(Path(self.temporary.name) / 'auth.db'),
            'FINANCE_AUTH_KEY_PATH': str(Path(self.temporary.name) / 'auth.key'),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for target in (main, store):
            context = patch.object(target, 'DB_PATH', self.path)
            context.start()
            self.addCleanup(context.stop)
        # Avoid startup lifespans: tests must not schedule financial network reads.
        self.client = TestClient(main.app, base_url='https://testserver')
        self.addCleanup(self.client.close)

    def test_all_bank_endpoints_require_auth_and_do_not_call_provider(self):
        with patch('app.auth.session_valid', return_value=False), \
                patch.object(provider, 'read') as read:
            for method, path, body in [
                ('GET', '/api/banking', None),
                ('GET', '/api/banking/accounts/42', None),
                ('GET', '/api/merchants/1/activity', None),
                ('GET', '/api/transactions/1/review', None),
                ('POST', '/api/banking/sync', {}),
                ('POST', '/api/banking/accounts/42/mapping', {'create_new': True}),
                ('POST', '/api/banking/accounts/42/balance-rule', {'balance_type': 'reported'}),
                ('POST', '/api/banking/reviews/1', {'action': 'ignore'}),
            ]:
                with self.subTest(path=path):
                    response = self.client.request(method, path, json=body,
                                                   headers={'X-Ledger-Request': '1'})
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.headers['cache-control'], 'no-store')
                    self.assertNotIn(self.secret, response.text)
            read.assert_not_called()

    def test_sync_csrf_origin_and_host_boundaries_precede_network(self):
        with patch('app.auth.session_valid', return_value=True), \
                patch.object(provider, 'read') as read:
            cases = [({}, 403),
                     ({'X-Ledger-Request': '1', 'Origin': 'https://attacker.invalid'}, 403),
                     ({'X-Ledger-Request': '1', 'Sec-Fetch-Site': 'cross-site'}, 403),
                     ({'X-Ledger-Request': '1', 'Host': 'attacker.invalid'}, 400)]
            for headers, status in cases:
                with self.subTest(headers=list(headers)):
                    response = self.client.post('/api/banking/sync', json={}, headers=headers)
                    self.assertEqual(response.status_code, status)
            read.assert_not_called()

    def test_cached_views_hide_raw_payload_and_credentials_and_make_no_network_request(self):
        raw_marker = 'synthetic-provider-private-raw-value'
        conn = self.store.database(self.path)
        account = provider.list_accounts({'accounts': [{
            'id': 42, 'name': 'Synthetic account 12345678901', 'institution_name': 'Synthetic Bank',
            'provider': 'synthetic', 'status': 'ACTIVE', 'private': raw_marker,
        }], 'total': 1})
        with conn:
            self.store.discover_accounts(conn, account, self.store.now())
            self.store.record_account_result(conn, '42', {
                'balance': {'amount': 100, 'currency': 'GBP'},
                'transactions': provider.transactions({'transactions': [{
                    'id': 'txn-safe', 'accountId': 42, 'amount': -1, 'currency': 'GBP',
                    'date': '2026-09-15', 'merchant': 'Synthetic merchant', 'description': 'Synthetic transaction',
                    'isPending': False, 'private': raw_marker,
                }], 'total': 1}, '42'), 'errors': [],
            }, self.store.now())
        conn.close()
        with patch('app.auth.session_valid', return_value=True), patch.object(provider, 'read') as read:
            for path in ['/api/banking', '/api/banking/accounts/42']:
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.headers['cache-control'], 'no-store')
                for private in [self.secret, raw_marker, '12345678901']:
                    self.assertNotIn(private, response.text)
            read.assert_not_called()

    def test_validation_and_parameterized_lookup_do_not_echo_secrets_or_change_tables(self):
        with patch('app.auth.session_valid', return_value=True), patch.object(provider, 'read') as read:
            response = self.client.post('/api/banking/accounts/42/mapping',
                json={'account_id': self.secret * 20}, headers={'X-Ledger-Request': '1'})
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(self.secret, response.text)
            response = self.client.get('/api/banking/accounts/42%27%20OR%201=1--')
            self.assertEqual(response.status_code, 404)
            for query in ['?limit=501', '?offset=-1', '?status=unexpected', '?date_from=2026-02-30']:
                self.assertEqual(self.client.get('/api/banking/accounts/42' + query).status_code, 422)
            read.assert_not_called()
        conn = self.store.database(self.path)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM bank_connections').fetchone()[0], 0)
        conn.close()


if __name__ == '__main__':
    unittest.main()

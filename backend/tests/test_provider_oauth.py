"""Consumer consent tests: mock every provider request; use synthetic private stores."""
from contextlib import closing
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
import pyotp

from app import auth, db, product_connections as connections, provider_oauth as oauth
from app.runtime import RequestBoundaryMiddleware
from app.security import RequestSizeMiddleware, SecurityHeadersMiddleware
from scripts.installation import initialize


class ConsumerAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / 'data' / 'finance.db'
        self.auth_db, self.key = self.db.with_name('auth.db'), self.root / 'keys' / 'auth.key'
        self.env = patch.dict(os.environ, {
            'FINANCE_AUTH_ENABLED': 'true', 'FINANCE_COOKIE_SECURE': 'true',
            'FINANCE_AUTH_DB_PATH': str(self.auth_db), 'FINANCE_AUTH_KEY_PATH': str(self.key),
            'FINANCE_PUBLIC_ORIGIN': 'https://testserver', 'FINANCE_ALLOWED_HOSTS': 'testserver',
            'LUNCH_FLOW_CLIENT_ID': 'synthetic-lf-app', 'LUNCH_FLOW_CLIENT_SECRET': 'synthetic-lf-secret',
            'SNAPTRADE_COMMERCIAL_ID': 'synthetic-st-app', 'SNAPTRADE_COMMERCIAL_KEY': 'synthetic-st-secret',
            'LUNCH_FLOW_API': '', 'SNAPTRADE_ID': '', 'SNAPTRADE_KEY': ''})
        self.env.start()
        self.db_override = patch('app.db.DB_PATH', self.db); self.db_override.start()
        initialize(self.db, self.root / 'uploads', self.auth_db, self.key)
        self.app = FastAPI()
        self.app.include_router(auth.router)
        self.app.include_router(oauth.router)
        self.app.include_router(oauth.callback_router)
        self.app.include_router(connections.router)
        self.app.add_middleware(RequestSizeMiddleware)
        self.app.add_middleware(auth.AuthenticationMiddleware)
        self.app.add_middleware(RequestBoundaryMiddleware)
        self.app.add_middleware(SecurityHeadersMiddleware)
        self.client = TestClient(self.app, base_url='https://testserver')
        self.token = self.session()
        self.client.cookies.set(auth.COOKIE, self.token)

    def tearDown(self):
        self.client.close()
        self.db_override.stop()
        self.env.stop()
        self.temp.cleanup()

    def session(self):
        with auth.database() as conn:
            value = auth.create_session(conn, Response()); conn.commit()
        return value

    def post(self, action, body):
        return self.client.post('/api/connections/authorization/' + action, json=body,
                                headers={'X-Ledger-Request': '1'})

    def begin(self, provider='lunchflow', **kwargs):
        response = self.post('start', {'provider': provider, 'label': 'My synthetic provider', **kwargs})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        if provider == 'lunchflow':
            result['state'] = parse_qs(urlsplit(result['authorization_url']).query)['state'][0]
        return result

    def tokens(self, user='synthetic-lf-user', access='synthetic-access'):
        return {'user_id': user, 'access_token': access, 'refresh_token': 'synthetic-refresh', 'expires_in': 3600, 'token_type': 'Bearer'}

    def finish(self, result):
        return self.post('complete', {'provider': 'lunchflow', 'state': result['state'], 'code': 'synthetic-code'})

    def control(self, operation, config, **kwargs):
        if operation == 'register':
            return {'userId': kwargs['user_id'], 'userSecret': 'synthetic-user-secret'}
        self.portal_arguments = kwargs
        self.portal_state = parse_qs(urlsplit(kwargs['custom_redirect']).query)['state'][0]
        return {'redirectURI': 'https://app.snaptrade.com/snapTrade/redeemToken?token=synthetic-portal-token'}

    def test_discovery_requires_secure_operator_configuration_and_never_exposes_it(self):
        result = self.client.get('/api/connections/authorization/providers')
        self.assertEqual(len(result.json()['providers']), 2)
        self.assertNotIn('synthetic-', result.text)
        with patch.dict(os.environ, {'LUNCH_FLOW_CLIENT_SECRET': ''}):
            self.assertEqual(oauth.available_methods('lunchflow'), [])
        with patch.dict(os.environ, {'FINANCE_COOKIE_SECURE': 'false'}):
            self.assertEqual(oauth.available_methods('snaptrade'), [])
        with patch.dict(os.environ, {'FINANCE_PUBLIC_ORIGIN': 'https://evil.example'}):
            self.assertEqual(oauth.available_methods('lunchflow'), [])

    def test_lunchflow_state_exchange_secret_encryption_and_replay_rejection(self):
        result = self.begin()
        query = parse_qs(urlsplit(result['authorization_url']).query)
        self.assertEqual(query['redirect_uri'], ['https://testserver/connect/callback/lunchflow'])
        self.assertEqual(urlsplit(result['authorization_url']).hostname, 'www.lunchflow.app')
        self.assertNotIn('synthetic-lf-secret', result['authorization_url'])
        with closing(db.connect(self.db)) as conn, conn:
            row = conn.execute('SELECT * FROM provider_authorizations').fetchone()
            self.assertNotEqual(row['state_digest'], result['state'])
            self.assertNotEqual(row['session_digest'], self.token)
        with patch.object(oauth, '_lf_request', return_value=self.tokens()) as exchange:
            response = self.finish(result)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['connection']['auth_method'], 'lunchflow_oauth')
            self.assertNotIn('synthetic-access', response.text)
            exchange.assert_called_once()
            self.assertEqual(exchange.call_args.kwargs['payload']['redirect_uri'], query['redirect_uri'][0])
            self.assertEqual(self.finish(result).status_code, 409)
            exchange.assert_called_once()
        with closing(db.connect(self.db)) as conn, conn:
            stored = conn.execute('SELECT secrets FROM provider_connections WHERE id=?', (result['connection_id'],)).fetchone()[0]
            self.assertNotIn(b'synthetic-access', stored)
            self.assertEqual(json.loads(auth.cipher().decrypt(stored))['access_token'], 'synthetic-access')

    def test_callback_state_bound_to_session_and_expiry(self):
        result = self.begin()
        self.client.cookies.set(auth.COOKIE, self.session())
        with patch.object(oauth, '_lf_request', return_value=self.tokens()) as exchange:
            self.assertEqual(self.finish(result).status_code, 409)
            exchange.assert_not_called()
            self.client.cookies.set(auth.COOKIE, self.token)
            with closing(db.connect(self.db)) as conn, conn:
                conn.execute('UPDATE provider_authorizations SET expires_at=0'); conn.commit()
            self.assertEqual(self.finish(result).status_code, 409)
            exchange.assert_not_called()

    def test_disconnect_or_edit_invalidates_inflight_authorization(self):
        result = self.begin()
        response = self.client.post('/api/connections/' + result['connection_id'] + '/disconnect', headers={'X-Ledger-Request': '1'})
        self.assertEqual(response.status_code, 200, response.text)
        with patch.object(oauth, '_lf_request', return_value=self.tokens()) as exchange:
            self.assertEqual(self.finish(result).status_code, 409)
            exchange.assert_not_called()

    def test_csrf_size_limit_and_validation_cannot_echo_credentials(self):
        body = {'provider': 'lunchflow', 'label': 'synthetic', 'redirect_uri': 'https://evil.example/secret'}
        self.assertEqual(self.client.post('/api/connections/authorization/start', json=body).status_code, 403)
        invalid = self.post('start', body)
        self.assertEqual(invalid.status_code, 422)
        self.assertNotIn('evil.example', invalid.text)
        self.assertEqual(self.post('complete', {'provider': 'lunchflow', 'state': 'x' * 9000}).status_code, 413)
        cross_origin = self.client.post('/api/connections/authorization/start', json={'provider': 'lunchflow', 'label': 'Bank'},
            headers={'X-Ledger-Request': '1', 'Origin': 'https://evil.example'})
        self.assertEqual(cross_origin.status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.post('start', {'provider': 'lunchflow', 'label': 'Bank'}).status_code, 401)

    def test_snaptrade_registration_readonly_portal_and_server_side_completion(self):
        with patch.object(oauth, '_snaptrade_control', side_effect=self.control) as control:
            result = self.begin('snaptrade')
        self.assertEqual(control.call_count, 2)
        self.assertEqual(self.portal_arguments['connection_type'], 'read')
        self.assertTrue(self.portal_arguments['immediate_redirect'])
        self.assertEqual(urlsplit(self.portal_arguments['custom_redirect']).path, '/connect/callback/snaptrade')
        with patch.object(connections.ProviderAdapter, 'validate', return_value=1) as validate:
            completed = self.post('complete', {'provider': 'snaptrade', 'state': self.portal_state})
            self.assertEqual(completed.status_code, 200, completed.text)
            self.assertEqual(validate.call_args.args[0]['auth_method'], 'snaptrade_commercial')
            self.assertEqual(validate.call_args.args[0]['user_secret'], 'synthetic-user-secret')
        self.assertNotIn('synthetic-user-secret', completed.text)

    def test_portal_redirect_and_control_transport_cannot_escape_allowlist(self):
        def bad_portal(operation, config, **kwargs):
            if operation == 'register': return self.control(operation, config, **kwargs)
            return {'redirectURI': 'https://app.snaptrade.com.evil.example/steal'}
        with patch.object(oauth, '_snaptrade_control', side_effect=bad_portal):
            self.assertEqual(self.post('start', {'provider': 'snaptrade', 'label': 'Broker'}).status_code, 409)
        transport = Mock(return_value=Mock(data=b'{}'))
        permitted = oauth.restrict_control_transport(transport, '/snapTrade/login')
        for method, url in [('POST', 'https://api.snaptrade.com/trade/placeOrder'),
                            ('POST', 'https://evil.example/snapTrade/login'),
                            ('GET', 'https://api.snaptrade.com/snapTrade/login'),
                            ('POST', 'http://api.snaptrade.com/snapTrade/login')]:
            with self.assertRaises(ValueError): permitted(method, url)
        transport.assert_not_called()
        permitted('POST', 'https://api.snaptrade.com/snapTrade/login')
        self.assertFalse(transport.call_args.kwargs['redirect'])
        self.assertFalse(transport.call_args.kwargs['retries'])

    def test_lunchflow_refresh_rotates_encrypted_tokens_under_lease(self):
        result = self.begin()
        with patch.object(oauth, '_lf_request', return_value=self.tokens()):
            self.assertEqual(self.finish(result).status_code, 200)
        with closing(db.connect(self.db)) as conn, conn:
            row = conn.execute('SELECT * FROM provider_connections WHERE id=?', (result['connection_id'],)).fetchone()
            values = oauth._decode(row); values['expires_at'] = 0
            oauth._save(conn, result['connection_id'], values); conn.commit()
        with patch.object(oauth, '_lf_request', return_value=self.tokens(access='rotated-access')) as refresh, \
                patch.object(connections.ProviderAdapter, 'validate', return_value=1) as validate:
            response = connections.perform_operation(result['connection_id'], 'validate', path=self.db)
            self.assertEqual(response['connection']['status'], 'ready')
            self.assertEqual(refresh.call_args.kwargs['payload']['grant_type'], 'refresh_token')
            self.assertEqual(validate.call_args.args[0]['access_token'], 'rotated-access')
        with closing(db.connect(self.db)) as conn, conn:
            saved = oauth._decode(conn.execute('SELECT * FROM provider_connections WHERE id=?', (result['connection_id'],)).fetchone())
            self.assertEqual(saved['access_token'], 'rotated-access')
            self.assertGreater(saved['expires_at'], time.time())

    def test_changed_lunchflow_subject_preserves_saved_credentials(self):
        result = self.begin()
        with patch.object(oauth, '_lf_request', return_value=self.tokens()):
            self.assertEqual(self.finish(result).status_code, 200)
        repeated = self.begin(connection_id=result['connection_id'])
        with patch.object(oauth, '_lf_request', return_value=self.tokens(user='a-different-user', access='different-access')):
            self.assertEqual(self.finish(repeated).status_code, 409)
        with closing(db.connect(self.db)) as conn, conn:
            saved = oauth._decode(conn.execute('SELECT * FROM provider_connections WHERE id=?', (result['connection_id'],)).fetchone())
            self.assertEqual(saved['access_token'], 'synthetic-access')

    def test_concurrent_callback_exchanges_code_only_once(self):
        started = self.begin()
        def attempt(_):
            with TestClient(self.app, base_url='https://testserver') as client:
                client.cookies.set(auth.COOKIE, self.token)
                return client.post('/api/connections/authorization/complete', json={
                    'provider': 'lunchflow', 'state': started['state'], 'code': 'synthetic-code'},
                    headers={'X-Ledger-Request': '1'}).status_code
        with patch.object(oauth, '_lf_request', return_value=self.tokens()) as exchange:
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(sorted(pool.map(attempt, range(2))), [200, 409])
            exchange.assert_called_once()

    def test_refresh_failure_preserves_tokens_records_safe_error_and_releases_lease(self):
        started = self.begin()
        with patch.object(oauth, '_lf_request', return_value=self.tokens()):
            self.assertEqual(self.finish(started).status_code, 200)
        with closing(db.connect(self.db)) as conn, conn:
            row = conn.execute('SELECT * FROM provider_connections WHERE id=?', (started['connection_id'],)).fetchone()
            values = oauth._decode(row); values['expires_at'] = 0
            oauth._save(conn, started['connection_id'], values); conn.commit()
            old = conn.execute('SELECT secrets FROM provider_connections WHERE id=?', (started['connection_id'],)).fetchone()[0]
        with patch.object(oauth, '_lf_request', side_effect=RuntimeError('secret provider response must never leak')):
            result = connections.perform_operation(started['connection_id'], 'validate', path=self.db)
        self.assertEqual(result['connection']['status'], 'error')
        self.assertIn('Reconnect', result['connection']['last_error'])
        self.assertNotIn('secret provider response', result['connection']['last_error'])
        with closing(db.connect(self.db)) as conn, conn:
            row = conn.execute('SELECT * FROM provider_connections WHERE id=?', (started['connection_id'],)).fetchone()
            self.assertEqual(row['status'], 'error')
            self.assertIsNotNone(row['last_checked_at'])
            self.assertEqual(row['secrets'], old)
            self.assertNotIn('secret provider response', row['last_error'])
            self.assertIsNone(conn.execute('SELECT owner FROM provider_connection_jobs').fetchone()[0])

    def test_disconnected_snaptrade_uses_new_scope_and_retains_previous_record(self):
        with patch.object(oauth, '_snaptrade_control', side_effect=self.control):
            previous = self.begin('snaptrade')
            self.assertEqual(self.client.post('/api/connections/' + previous['connection_id'] + '/disconnect',
                headers={'X-Ledger-Request': '1'}).status_code, 200)
            current = self.begin('snaptrade', connection_id=previous['connection_id'])
        self.assertNotEqual(previous['connection_id'], current['connection_id'])
        with closing(db.connect(self.db)) as conn, conn:
            row = conn.execute('SELECT * FROM provider_connections WHERE id=?', (previous['connection_id'],)).fetchone()
            self.assertIsNone(row['secrets'])
            self.assertEqual(row['status'], 'disconnected')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM provider_connections').fetchone()[0], 2)

    def test_lunchflow_transport_retains_only_documented_read_resources(self):
        with patch.object(oauth, '_lf_request', return_value={'transactions': [], 'total': 0}) as read:
            oauth.platform_read('transactions', 'a1', date_from='2026-01-01', date_to='2026-01-31', access_token='synthetic')
            self.assertEqual(read.call_args.args, ('GET', '/api/platform/v1/accounts/a1/transactions'))
            self.assertEqual(read.call_args.kwargs['query'], {'include_pending': 'true', 'from': '2026-01-01', 'to': '2026-01-31'})
            with self.assertRaises(ValueError): oauth.platform_read('delete', 'a1', access_token='synthetic')
            with self.assertRaises(ValueError): oauth.platform_read('balance', '../../users', access_token='synthetic')
        with self.assertRaises(ValueError): oauth._lf_request('POST', '/api/platform/v1/users', payload={})
        with self.assertRaises(ValueError): oauth._lf_request('GET', 'https://evil.example', access_token='synthetic')


if __name__ == '__main__':
    unittest.main()

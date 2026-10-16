"""Real SDK/HTTP serialization and household boundaries, with no outbound sockets.

Only the final transport is replaced. Credentials, API routing, encryption,
database writes, provider normalization and SnapTrade signing are real code.
All amounts, identities and responses below are synthetic.
"""
from contextlib import ExitStack, closing, contextmanager
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import InvalidToken
from fastapi import Response
from fastapi.testclient import TestClient
from urllib3.response import HTTPResponse

from app import auth, db, main, product_connections as connections
from app import provider_oauth as oauth, snaptrade_client
from scripts.installation import initialize


class ProviderWireContracts(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='ledger-wire-')))
        self.enterContext(patch.object(socket.socket, 'connect', side_effect=AssertionError('Outbound network forbidden')))
        self.enterContext(patch.dict(os.environ, {
            'FINANCE_AUTH_ENABLED': 'true', 'FINANCE_COOKIE_SECURE': 'true',
            'FINANCE_DEPLOYMENT': 'local', 'FINANCE_PUBLIC_ORIGIN': 'https://testserver',
            'FINANCE_ALLOWED_HOSTS': 'testserver', 'LUNCH_FLOW_API': '',
            'SNAPTRADE_ID': '', 'SNAPTRADE_KEY': '',
            'LUNCH_FLOW_CLIENT_ID': 'synthetic-platform-id',
            'LUNCH_FLOW_CLIENT_SECRET': 'synthetic-platform-secret',
            'SNAPTRADE_COMMERCIAL_ID': 'synthetic-commercial-id',
            'SNAPTRADE_COMMERCIAL_KEY': 'synthetic-commercial-secret',
            'LUNCH_FLOW_SYNC_SECONDS': '0', 'SNAPTRADE_SYNC_SECONDS': '0',
            'INVESTMENT_PRICE_REFRESH_SECONDS': '0',
        }))

    @contextmanager
    def household(self, name):
        root = self.folder / name
        path, key = root / 'data' / 'finance.db', root / 'keys' / 'auth.key'
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {
                'FINANCE_AUTH_DB_PATH': str(path.with_name('auth.db')),
                'FINANCE_AUTH_KEY_PATH': str(key),
            }))
            stack.enter_context(patch.object(db, 'DB_PATH', path))
            stack.enter_context(patch.object(main, 'DB_PATH', path))
            if not path.exists():
                initialize(path, root / 'uploads', path.with_name('auth.db'), key)
            client = TestClient(main.app, base_url='https://testserver')
            stack.callback(client.close)
            with auth.database() as conn:
                token = auth.create_session(conn, Response())
                conn.commit()
            client.cookies.set(auth.cookie_name(), token)
            yield client, path, token

    def post(self, client, path, body=None, *, status=200):
        response = client.post(path, json=body, headers={'X-Ledger-Request': '1'})
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def test_snaptrade_installed_sdk_signs_commercial_control_and_scoped_reads(self):
        observed = []
        user = 'synthetic-user'
        config = {'client_id': 'synthetic-commercial-id', 'client_secret': 'synthetic-commercial-secret'}

        def wire(method, url, **kwargs):
            parsed = urlsplit(url)
            query = parse_qs(parsed.query)
            observed.append((method, parsed.path, query, kwargs))
            self.assertEqual(parsed.hostname, 'api.snaptrade.com')
            self.assertEqual(query['clientId'], [config['client_id']])
            self.assertTrue(query.get('timestamp'))
            headers = {key.lower(): value for key, value in kwargs['headers'].items()}
            self.assertTrue(headers.get('signature'))
            self.assertFalse(kwargs['redirect'])
            self.assertFalse(kwargs['retries'])
            if parsed.path.endswith('/registerUser'):
                self.assertEqual(json.loads(kwargs['body'])['userId'], user)
                body = {'userId': user, 'userSecret': 'synthetic-user-secret'}
            elif parsed.path.endswith('/login'):
                payload = json.loads(kwargs['body'])
                self.assertEqual(payload['connectionType'], 'read')
                self.assertTrue(payload['immediateRedirect'])
                self.assertEqual(payload['customRedirect'], 'https://testserver/connect/callback/snaptrade?state=synthetic')
                self.assertEqual(query['userId'], [user])
                self.assertEqual(query['userSecret'], ['synthetic-user-secret'])
                body = {'redirectURI': 'https://app.snaptrade.com/snapTrade/redeemToken?token=synthetic'}
            elif parsed.path.endswith('/accounts'):
                self.assertEqual(method, 'GET')
                self.assertEqual(query['userId'], [user])
                self.assertEqual(query['userSecret'], ['synthetic-user-secret'])
                body = []
            else:
                self.fail('Unexpected SDK operation')
            return HTTPResponse(body=json.dumps(body).encode(), status=200, headers={'Content-Type': 'application/json'})

        with patch('urllib3.PoolManager.request', side_effect=wire):
            registered = oauth._snaptrade_control('register', config, user_id=user)
            oauth._snaptrade_control('portal', config, user_id=user, user_secret=registered['userSecret'],
                connection_type='read', immediate_redirect=True,
                custom_redirect='https://testserver/connect/callback/snaptrade?state=synthetic')
            credentials = {'auth_method': 'snaptrade_commercial', 'client_id': config['client_id'],
                'consumer_key': config['client_secret'], 'user_id': user, 'user_secret': registered['userSecret']}
            self.assertEqual(connections.PROVIDERS['snaptrade'].validate(credentials), 0)
        self.assertEqual([entry[0] for entry in observed], ['POST', 'POST', 'GET'])

    def test_snaptrade_personal_sdk_omits_commercial_user_credentials(self):
        def wire(method, url, **kwargs):
            query = parse_qs(urlsplit(url).query)
            self.assertEqual(method, 'GET')
            self.assertNotIn('userId', query)
            self.assertNotIn('userSecret', query)
            self.assertEqual(query['clientId'], ['synthetic-personal-id'])
            self.assertTrue({k.lower(): v for k, v in kwargs['headers'].items()}['signature'])
            return HTTPResponse(body=b'[]', status=200, headers={'Content-Type': 'application/json'})
        with patch('urllib3.PoolManager.request', side_effect=wire) as transport:
            self.assertEqual(snaptrade_client.call('accounts', credentials={
                'client_id': 'synthetic-personal-id', 'consumer_key': 'synthetic-personal-secret'}), [])
        self.assertEqual(transport.call_count, 1)

    def test_lunchflow_authorization_sync_and_refresh_use_real_http_request_shapes(self):
        calls = []

        def wire(request, timeout):
            parsed = urlsplit(request.full_url)
            headers = {k.lower(): v for k, v in request.header_items()}
            calls.append((request.method, parsed.path))
            self.assertEqual(parsed.hostname, 'www.lunchflow.app')
            self.assertEqual(timeout, 20)
            if request.method == 'POST':
                self.assertEqual(parsed.path, '/api/platform/oauth/token')
                payload = json.loads(request.data)
                self.assertEqual(payload['client_id'], 'synthetic-platform-id')
                self.assertEqual(payload['client_secret'], 'synthetic-platform-secret')
                refresh = payload['grant_type'] == 'refresh_token'
                if refresh:
                    self.assertEqual(payload['refresh_token'], 'synthetic-refresh')
                else:
                    self.assertEqual(payload['redirect_uri'], 'https://testserver/connect/callback/lunchflow')
                    self.assertEqual(payload['code'], 'synthetic-code')
                body = {'user_id': 'synthetic-bank-user', 'access_token': 'synthetic-rotated' if refresh else 'synthetic-access',
                        'refresh_token': 'synthetic-refresh', 'expires_in': 3600, 'token_type': 'Bearer'}
            else:
                self.assertEqual(headers['authorization'], 'Bearer synthetic-rotated')
                if parsed.path.endswith('/accounts'):
                    body = {'accounts': [{'id': 'bank-eur', 'name': 'Holiday account', 'institution_name': 'Synthetic Bank', 'currency': 'EUR'}]}
                elif parsed.path.endswith('/balance'):
                    body = {'balance': {'amount': 200, 'currency': 'EUR'}}
                elif parsed.path.endswith('/transactions'):
                    self.assertEqual(parse_qs(parsed.query)['include_pending'], ['true'])
                    body = {'transactions': [{'id': 'coffee', 'accountId': 'bank-eur', 'date': '2026-08-01',
                        'description': 'Synthetic café', 'amount': -4.5, 'currency': 'EUR', 'isPending': False}]}
                else:
                    self.fail('Unexpected Lunch Flow operation')
            return io.BytesIO(json.dumps(body).encode())

        with self.household('bank') as (client, path, _), patch('urllib.request.OpenerDirector.open', side_effect=wire):
            started = self.post(client, '/api/connections/authorization/start', {'provider': 'lunchflow', 'label': 'Holiday bank'})
            state = parse_qs(urlsplit(started['authorization_url']).query)['state'][0]
            self.post(client, '/api/connections/authorization/complete', {'provider': 'lunchflow', 'state': state, 'code': 'synthetic-code'})
            with closing(db.connect(path)) as conn, conn:
                row = conn.execute('SELECT * FROM provider_connections WHERE id=?', (started['connection_id'],)).fetchone()
                values = oauth._decode(row)
                values['expires_at'] = 0
                oauth._save(conn, started['connection_id'], values)
                conn.commit()
            result = self.post(client, '/api/connections/' + started['connection_id'] + '/sync')
            self.assertIsNone(result['connection']['last_error'])
            self.assertEqual(result['connection']['status'], 'ready')
            with closing(db.connect(path)) as conn, conn:
                row = conn.execute('SELECT amount,currency FROM bank_transactions').fetchone()
                self.assertEqual(tuple(row), (-4.5, 'EUR'))
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 0)
                saved = conn.execute('SELECT secrets FROM provider_connections').fetchone()[0]
                self.assertNotIn(b'synthetic-rotated', saved)
            self.assertNotIn('synthetic-rotated', client.get('/api/connections').text)
        self.assertEqual(calls.count(('POST', '/api/platform/oauth/token')), 2)
        self.assertEqual(len(calls), 5)

    def test_households_do_not_share_sessions_saved_connections_or_keys(self):
        with self.household('one') as (client, path, first_token):
            created = self.post(client, '/api/connections', {'provider': 'lunchflow', 'label': 'Only household one',
                'credentials': {'api_key': 'synthetic-household-one-secret'}}, status=201)
            with closing(db.connect(path)) as conn, conn:
                ciphertext = conn.execute('SELECT secrets FROM provider_connections').fetchone()[0]
            first_connections = client.get('/api/connections').json()['connections']
            self.assertEqual(len(first_connections), 1)
        with self.household('two') as (client, _, second_token):
            self.assertEqual(client.get('/api/connections').json()['connections'], [])
            self.assertEqual(client.post('/api/connections/' + created['id'] + '/sync',
                headers={'X-Ledger-Request': '1'}).status_code, 404)
            with self.assertRaises(InvalidToken):
                auth.cipher().decrypt(ciphertext)
            client.cookies.set(auth.cookie_name(), first_token)
            self.assertEqual(client.get('/api/connections').status_code, 401)
            client.cookies.set(auth.cookie_name(), second_token)
            self.assertEqual(client.get('/api/connections').status_code, 200)
        with self.household('one') as (client, _, __):
            self.assertEqual(client.get('/api/connections').json()['connections'], first_connections)

    def test_browser_fixture_clears_inherited_live_provider_configuration(self):
        script = Path(__file__).resolve().parents[2] / 'scripts' / 'e2e_server.py'
        spec = importlib.util.spec_from_file_location('isolated_browser_fixture', script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        keys = ('LUNCH_FLOW_API', 'SNAPTRADE_ID', 'SNAPTRADE_KEY', 'LUNCH_FLOW_CLIENT_ID',
                'LUNCH_FLOW_CLIENT_SECRET', 'SNAPTRADE_COMMERCIAL_ID', 'SNAPTRADE_COMMERCIAL_KEY')
        with patch.dict(os.environ, dict.fromkeys(keys, 'inherited-do-not-use')):
            module.isolate_environment()
            self.assertTrue(all(os.environ[key] == '' for key in keys))
            self.assertEqual(os.environ['INVESTMENT_PRICE_REFRESH_SECONDS'], '0')


if __name__ == '__main__':
    unittest.main()

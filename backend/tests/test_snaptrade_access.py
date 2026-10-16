"""Private portfolio access through the real middleware; no live broker calls."""
import io
import logging
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import auth, main, snaptrade_routes, snaptrade_store


class ConnectedPortfolioAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        self.auth_db = folder / 'data' / 'auth.db'
        self.environment = patch.dict(os.environ, {
            'FINANCE_DEPLOYMENT': 'public',
            'FINANCE_AUTH_ENABLED': 'true',
            'FINANCE_COOKIE_SECURE': 'true',
            'FINANCE_PUBLIC_ORIGIN': 'https://testserver',
            'FINANCE_ALLOWED_HOSTS': 'testserver,localhost,127.0.0.1',
            'FINANCE_AUTH_DB_PATH': str(self.auth_db),
            'FINANCE_AUTH_KEY_PATH': str(folder / 'keys' / 'auth.key'),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        finance = patch.object(main, 'DB_PATH', folder / 'unused-finance.db')
        finance.start()
        self.addCleanup(finance.stop)
        cached_finance = patch.object(snaptrade_store.db, 'DB_PATH', folder / 'unused-finance.db')
        cached_finance.start()
        self.addCleanup(cached_finance.stop)
        cooldown = patch.object(snaptrade_routes, '_next_read', 0.0)
        cooldown.start()
        self.addCleanup(cooldown.stop)
        configured = patch.object(snaptrade_routes, 'configured', return_value=True)
        self.configured = configured.start()
        self.addCleanup(configured.stop)
        fetch = patch.object(snaptrade_routes, 'fetch_portfolio', return_value={
            'configured': True, 'accounts': [], 'connections': {'data': []},
            'retrieved_at': '2026-09-18T12:00:00+00:00', 'error': None,
        })
        self.fetch = fetch.start()
        self.addCleanup(fetch.stop)
        self.client = TestClient(main.app, base_url='https://testserver')
        self.addCleanup(self.client.close)

    def established_session(self, *, created=None, seen=None):
        # Existing auth tests exercise password + TOTP issuance. This fixture
        # represents its established, hashed, unexpired session, not a bypass.
        token = 'synthetic-connected-portfolio-session'
        now = time.time()
        with auth.database() as conn:
            conn.execute('INSERT OR REPLACE INTO sessions VALUES(?,?,?)', (
                auth.digest(token), now if created is None else created,
                now if seen is None else seen,
            ))
            conn.commit()
        self.client.cookies.set(auth.cookie_name(), token)

    def assert_private_response(self, response):
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertNotIn('access-control-allow-origin', response.headers)
        self.assertIn('content-security-policy', response.headers)

    def test_anonymous_and_forged_forwarding_headers_never_fetch(self):
        for headers in ({}, {'X-Forwarded-For': '192.168.50.10',
                             'X-Real-IP': '127.0.0.1'}):
            with self.subTest(headers=headers):
                response = self.client.get('/api/investments/connected', headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assert_private_response(response)
        self.fetch.assert_not_called()
        self.configured.assert_not_called()
        self.assertFalse(self.auth_db.exists())

    def test_established_session_reads_and_logout_revokes_access(self):
        self.established_session()
        response = self.client.get('/api/investments/connected')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['accounts'], [])
        self.assert_private_response(response)
        self.fetch.assert_called_once_with()
        self.assertEqual(self.client.post('/api/auth/logout', headers={
            'X-Ledger-Request': '1',
        }).status_code, 200)
        self.fetch.reset_mock()
        response = self.client.get('/api/investments/connected')
        self.assertEqual(response.status_code, 401)
        self.assert_private_response(response)
        self.fetch.assert_not_called()

    def test_saved_activity_routes_require_auth_and_never_refresh_or_seed(self):
        paths = ('/api/investments/connected/activities/accounts',
                 '/api/investments/connected/activities?account_id=saved-only')
        for path in paths:
            self.assertEqual(self.client.get(path).status_code, 401)
        self.established_session()
        empty = self.client.get(paths[0])
        self.assertEqual(empty.json(), {'accounts': []})
        self.assert_private_response(empty)
        self.assertFalse(snaptrade_store.db.DB_PATH.exists())
        snaptrade_store.save_portfolio({
            'configured': True, 'retrieved_at': '2026-09-18T12:00:00+00:00',
            'accounts': [{'id': 'saved-only', 'name': 'Synthetic saved account',
                          'institution': 'Example', 'is_paper': True,
                          'activities': {'status': 'ok', 'data': [
                              {'id': 'event', 'id_source': 'provider', 'type': 'DIVIDEND',
                               'amount': 1, 'currency': 'GBP'}]}}],
            'connections': {'data': []},
        })
        accounts = self.client.get(paths[0])
        self.assertEqual(accounts.status_code, 200)
        self.assertIs(accounts.json()['accounts'][0]['is_paper'], True)
        self.assertEqual(accounts.json()['accounts'][0]['stored_activities'], 1)
        saved = self.client.get(paths[1])
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()['activities'][0]['amount'], 1)
        self.assertFalse(saved.json()['coverage']['history_complete'])
        self.assert_private_response(saved)
        self.assertEqual(self.client.get(paths[1] + '&limit=201').status_code, 422)
        self.assertEqual(self.client.get(paths[1] + '&offset=-1').status_code, 422)
        self.assertEqual(self.client.post('/api/auth/logout', headers={'X-Ledger-Request': '1'}).status_code, 200)
        for path in paths:
            self.assertEqual(self.client.get(path).status_code, 401)
        self.fetch.assert_not_called()
        self.configured.assert_not_called()

    def test_expired_sessions_never_fetch(self):
        now = time.time()
        for times in ({'seen': now - auth.IDLE_SECONDS - 1},
                      {'created': now - auth.ABSOLUTE_SECONDS - 1}):
            with self.subTest(times=times):
                self.established_session(**times)
                response = self.client.get('/api/investments/connected')
                self.assertEqual(response.status_code, 401)
                self.assert_private_response(response)
        self.fetch.assert_not_called()

    def test_trusted_lan_reads_without_auth_store_or_cookie(self):
        with patch.dict(os.environ, {
            'FINANCE_DEPLOYMENT': 'trusted-lan', 'FINANCE_AUTH_ENABLED': 'false',
            'FINANCE_COOKIE_SECURE': 'false', 'FINANCE_PUBLIC_ORIGIN': '',
            'FINANCE_BIND_ADDRESS': '192.168.50.10',
        }):
            response = self.client.get('/api/investments/connected')
        self.assertEqual(response.status_code, 200)
        self.assert_private_response(response)
        self.assertNotIn('set-cookie', response.headers)
        self.assertFalse(self.auth_db.exists())
        self.fetch.assert_called_once_with()

    def test_cross_site_and_unconfigured_hosts_are_rejected_before_fetch(self):
        self.established_session()
        for deployment, enabled in (('public', 'true'), ('trusted-lan', 'false')):
            with patch.dict(os.environ, {
                'FINANCE_DEPLOYMENT': deployment, 'FINANCE_AUTH_ENABLED': enabled,
            }):
                for headers, status in (({'Sec-Fetch-Site': 'cross-site'}, 403),
                                        ({'Host': 'untrusted.example'}, 400)):
                    with self.subTest(deployment=deployment, headers=headers):
                        response = self.client.get('/api/investments/connected', headers=headers)
                        self.assertEqual(response.status_code, status)
                        self.assert_private_response(response)
        self.fetch.assert_not_called()
        self.configured.assert_not_called()

    def test_unexpected_upstream_error_is_private_and_sanitized(self):
        self.established_session()
        private_text = 'synthetic-private-brokerage-response-and-key'
        self.fetch.side_effect = RuntimeError(private_text)
        log_output = io.StringIO()
        handler = logging.StreamHandler(log_output)
        logging.getLogger().addHandler(handler)
        try:
            response = self.client.get('/api/investments/connected')
        finally:
            logging.getLogger().removeHandler(handler)
        self.assertEqual(response.status_code, 502)
        self.assert_private_response(response)
        self.assertNotIn(private_text, response.text)
        self.assertNotIn(private_text, log_output.getvalue())
        self.assertEqual(response.json()['detail'],
                         'The portfolio could not be retrieved. Please try again later.')

    def test_cached_reads_are_instant_and_only_provider_refresh_is_limited(self):
        self.established_session()
        first = self.client.get('/api/investments/connected')
        second = self.client.get('/api/investments/connected')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()['cache']['is_cached'])
        self.assert_private_response(second)
        refresh = self.client.post('/api/investments/connected/refresh', headers={'X-Ledger-Request': '1'})
        self.assertEqual(refresh.status_code, 429)
        self.assertGreater(int(refresh.headers['retry-after']), 0)
        self.fetch.assert_called_once_with()

    def test_refresh_and_snapshot_routes_use_the_same_auth_and_request_boundary(self):
        for path in ('/api/investments/connected/snapshots', '/api/investments/connected/snapshots/1'):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.post('/api/investments/connected/refresh',
                                         headers={'X-Ledger-Request': '1'}).status_code, 401)
        self.established_session()
        self.assertEqual(self.client.post('/api/investments/connected/refresh').status_code, 403)
        self.assertEqual(self.client.post('/api/investments/connected/refresh', headers={
            'X-Ledger-Request': '1', 'Sec-Fetch-Site': 'cross-site'}).status_code, 403)
        self.fetch.assert_not_called()
        first = self.client.post('/api/investments/connected/refresh', headers={'X-Ledger-Request': '1'})
        self.assertEqual(first.status_code, 200)
        snapshots = self.client.get('/api/investments/connected/snapshots')
        self.assertEqual(snapshots.status_code, 200)
        self.assert_private_response(snapshots)
        snapshot_id = snapshots.json()['snapshots'][0]['id']
        detail = self.client.get(f'/api/investments/connected/snapshots/{snapshot_id}')
        self.assertEqual(detail.status_code, 200)
        self.assert_private_response(detail)


if __name__ == '__main__':
    unittest.main()

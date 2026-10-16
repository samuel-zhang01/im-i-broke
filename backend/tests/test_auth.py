"""Authentication tests use temporary stores and synthetic credentials only."""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
import pyotp
from app import auth
from app.main import app


class AuthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = 'a synthetic testing password 123!'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            'FINANCE_AUTH_ENABLED': 'true', 'FINANCE_COOKIE_SECURE': 'true',
            'FINANCE_AUTH_DB_PATH': str(Path(self.tmp.name) / 'auth.db'),
            'FINANCE_AUTH_KEY_PATH': str(Path(self.tmp.name) / 'auth.key'),
        })
        self.environment.start()
        self.finance_path = patch('app.main.DB_PATH', Path(self.tmp.name) / 'unused-finance.db')
        self.finance_path.start()
        self.client = TestClient(app, base_url='https://testserver')
        self.secret = pyotp.random_base32()
        self.codes = auth.enroll(self.password, self.secret, pyotp.TOTP(self.secret).now())
        with auth.database() as conn:
            conn.execute('UPDATE owner SET last_step=-1')
            conn.commit()

    def tearDown(self):
        self.client.close()
        self.finance_path.stop()
        self.environment.stop()
        self.tmp.cleanup()

    def login(self, code=None, password=None, client=None, remember=False):
        return (client or self.client).post('/api/auth/login', json={
            'password': password or self.password, 'code': code or pyotp.TOTP(self.secret).now(), 'remember': remember,
        }, headers={'X-Ledger-Request': '1'})

    def test_entire_api_and_docs_are_protected(self):
        for path in ['/api/summary', '/api/revision', '/api/transactions', '/docs', '/openapi.json', '/api/unknown']:
            self.assertEqual(self.client.get(path).status_code, 401, path)
        self.assertEqual(self.client.post('/api/auth/recovery-codes', json={}, headers={'X-Ledger-Request': '1'}).status_code, 401)
        self.assertEqual(self.client.get('/api/auth/status').json()['authenticated'], False)
        self.assertIn(self.client.get('/api/health').status_code, [200, 503])

    def test_password_and_second_factor_required_secure_cookie_and_logout(self):
        self.assertEqual(self.login(password='incorrect').status_code, 401)
        self.assertEqual(self.login(code='invalid').status_code, 401)
        result = self.login()
        self.assertEqual(result.status_code, 200)
        for flag in ['HttpOnly', 'Secure', 'SameSite=strict', 'Path=/']:
            self.assertIn(flag, result.headers['set-cookie'])
        self.assertEqual(result.headers['cache-control'], 'no-store')
        self.assertTrue(self.client.get('/api/auth/status').json()['authenticated'])
        token = self.client.cookies.get(auth.COOKIE)
        with auth.database() as conn:
            self.assertNotEqual(conn.execute('SELECT digest FROM sessions').fetchone()[0], token)
        self.assertEqual(self.client.post('/api/auth/logout', headers={'X-Ledger-Request': '1'}).status_code, 200)
        self.assertFalse(auth.session_valid(token))

    def test_totp_and_recovery_replay_and_wrong_password_do_not_consume_code(self):
        code = pyotp.TOTP(self.secret).now()
        self.assertEqual(self.login(code).status_code, 200)
        self.assertEqual(self.login(code).status_code, 401)
        self.assertEqual(self.login(self.codes[0], password='wrong').status_code, 401)
        self.assertEqual(self.login(self.codes[0]).status_code, 200)
        self.assertEqual(self.login(self.codes[0]).status_code, 401)
        with auth.database() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM recovery').fetchone()[0], 9)
            self.assertNotIn(self.codes[1], [r[0] for r in conn.execute('SELECT digest FROM recovery')])
            self.assertNotIn(self.secret.encode(), conn.execute('SELECT secret FROM owner').fetchone()[0])

    def test_persistent_throttling_ignores_forged_forwarding_headers(self):
        for _ in range(5):
            self.assertEqual(self.login(password='wrong').status_code, 401)
        another = TestClient(app, base_url='https://testserver', headers={'X-Forwarded-For': '192.0.2.100'})
        result = self.login(self.codes[0], client=another)
        self.assertEqual(result.status_code, 429)
        self.assertIn('retry-after', result.headers)
        with auth.database() as conn:
            conn.execute('UPDATE throttle SET until=?', (time.time() - 1,))
            conn.commit()
        self.assertEqual(self.login(self.codes[0]).status_code, 200)
        another.close()

    def test_csrf_missing_header_cross_origin_and_disallowed_host(self):
        body = {'password': self.password, 'code': self.codes[0]}
        self.assertEqual(self.client.post('/api/auth/login', json=body).status_code, 403)
        for origin in ['https://evil.example', 'null']:
            self.assertEqual(self.client.post('/api/auth/login', json=body,
                headers={'X-Ledger-Request': '1', 'Origin': origin}).status_code, 403)
        self.assertEqual(self.client.get('/api/auth/status', headers={'Host': 'evil.example'}).status_code, 400)
        result = self.client.options('/api/auth/login', headers={'Origin': 'http://localhost:5173',
            'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'x-ledger-request'})
        self.assertNotIn('access-control-allow-origin', result.headers)

    def test_idle_absolute_expiry_and_polling_does_not_refresh(self):
        self.assertEqual(self.login().status_code, 200)
        token = self.client.cookies.get(auth.COOKIE)
        with auth.database() as conn:
            seen = time.time() - 60
            conn.execute('UPDATE sessions SET seen=?', (seen,)); conn.commit()
        self.client.get('/api/auth/status')
        self.client.get('/api/revision')
        with auth.database() as conn:
            self.assertEqual(conn.execute('SELECT seen FROM sessions').fetchone()[0], seen)
            conn.execute('UPDATE sessions SET seen=?', (time.time() - auth.IDLE_SECONDS - 1,)); conn.commit()
        self.assertFalse(auth.session_valid(token))
        self.assertEqual(self.client.get('/api/summary').status_code, 401)
        with auth.database() as conn:
            conn.execute('UPDATE sessions SET created=?, seen=?', (time.time() - auth.ABSOLUTE_SECONDS - 1, time.time())); conn.commit()
        self.assertFalse(auth.session_valid(token))

    def test_remembered_session_survives_return_visit_but_has_both_expiry_limits(self):
        result = self.login(remember=True)
        self.assertEqual(result.status_code, 200)
        self.assertIn(f'Max-Age={auth.REMEMBER_ABSOLUTE_SECONDS}', result.headers['set-cookie'])
        token = self.client.cookies.get(auth.COOKIE)
        now = time.time()
        with auth.database() as conn:
            conn.execute('UPDATE sessions SET created=?, seen=?', (now - 3 * 86400, now - 2 * 86400))
            conn.commit()
        self.assertTrue(self.client.get('/api/auth/status').json()['authenticated'])
        with auth.database() as conn:
            conn.execute('UPDATE sessions SET seen=?', (now - auth.REMEMBER_IDLE_SECONDS - 1,))
            conn.commit()
        self.assertFalse(auth.session_valid(token))
        with auth.database() as conn:
            conn.execute('UPDATE sessions SET created=?, seen=?', (now - auth.REMEMBER_ABSOLUTE_SECONDS - 1, now))
            conn.commit()
        self.assertFalse(auth.session_valid(token))

    def test_remembered_session_revocation_and_standard_login_limits(self):
        self.assertEqual(self.login(remember=True).status_code, 200)
        token = self.client.cookies.get(auth.COOKIE)
        self.assertEqual(self.client.post('/api/auth/logout', headers={'X-Ledger-Request': '1'}).status_code, 200)
        self.assertFalse(auth.session_valid(token))
        with auth.database() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM remembered_sessions').fetchone()[0], 0)
        result = self.login(self.codes[0])
        self.assertIn(f'Max-Age={auth.ABSOLUTE_SECONDS}', result.headers['set-cookie'])
        with auth.database() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM remembered_sessions').fetchone()[0], 0)

    def test_session_activity_writes_are_coalesced(self):
        self.assertEqual(self.login().status_code, 200)
        token = self.client.cookies.get(auth.COOKIE)
        with auth.database() as conn:
            seen = time.time() - 10
            conn.execute('UPDATE sessions SET seen=?', (seen,))
            conn.commit()
        self.assertTrue(auth.session_valid(token))
        with auth.database() as conn:
            self.assertEqual(conn.execute('SELECT seen FROM sessions').fetchone()[0], seen)
            conn.execute('UPDATE sessions SET seen=?', (seen - 60,))
            conn.commit()
        self.assertTrue(auth.session_valid(token))
        with auth.database() as conn:
            self.assertGreater(conn.execute('SELECT seen FROM sessions').fetchone()[0], seen)

    def test_rotate_requires_both_factors_invalidates_old_codes_and_sessions(self):
        self.assertEqual(self.login().status_code, 200)
        token = self.client.cookies.get(auth.COOKIE)
        response = self.client.post('/api/auth/recovery-codes', json={'password': self.password, 'code': self.codes[0]}, headers={'X-Ledger-Request': '1'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['codes']), 10)
        self.assertFalse(auth.session_valid(token))
        self.assertEqual(self.login(self.codes[1]).status_code, 401)
        self.assertEqual(self.login(response.json()['codes'][0]).status_code, 200)

    def test_concurrent_recovery_use_succeeds_only_once(self):
        def attempt(_):
            with TestClient(app, base_url='https://testserver') as client:
                return self.login(self.codes[0], client=client).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(attempt, range(2))), [200, 401])

    def test_missing_configuration_fails_closed_no_public_enrollment(self):
        with auth.database() as conn:
            conn.execute('DELETE FROM owner'); conn.commit()
        self.assertFalse(self.client.get('/api/auth/status').json()['configured'])
        self.assertEqual(self.client.get('/api/summary').status_code, 401)
        self.assertEqual(self.client.post('/api/auth/setup', json={}, headers={'X-Ledger-Request': '1'}).status_code, 401)
        self.assertEqual(self.login(self.codes[0]).status_code, 401)

    def test_https_proxy_origin_must_be_explicit_and_key_loss_fails_closed(self):
        with patch.dict(os.environ, {'FINANCE_PUBLIC_ORIGIN': 'https://testserver'}):
            with TestClient(app, base_url='http://testserver') as proxy:
                response = proxy.post('/api/auth/login', json={
                    'password': self.password, 'code': self.codes[0]},
                    headers={'X-Ledger-Request': '1', 'Origin': 'https://testserver'})
                self.assertEqual(response.status_code, 200)
        self.assertEqual(auth.key_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(auth.auth_path().stat().st_mode & 0o777, 0o600)
        auth.key_path().unlink()
        with TestClient(app, base_url='https://testserver', raise_server_exceptions=False) as client:
            self.assertEqual(self.login(client=client).status_code, 500)
            self.assertEqual(client.get('/api/summary').status_code, 401)
        self.assertFalse(auth.key_path().exists())

    def test_audit_events_are_bounded_and_contain_no_credentials(self):
        self.assertEqual(self.login(password='wrong').status_code, 401)
        self.assertEqual(self.login().status_code, 200)
        with auth.database() as conn:
            labels = [row['event'] for row in conn.execute('SELECT * FROM security_events')]
            self.assertEqual(labels, ['owner_enrolled', 'login_rejected', 'login_accepted'])
            for _ in range(1005):
                auth.audit(conn, 'signed_out')
            conn.commit()
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM security_events').fetchone()[0], 1000)
            content = str([tuple(row) for row in conn.execute('SELECT * FROM security_events')])
            self.assertNotIn(self.password, content)
            self.assertNotIn(self.secret, content)
            self.assertNotIn(self.codes[0], content)

    def test_reset_revokes_sessions_and_old_codes(self):
        self.assertEqual(self.login().status_code, 200)
        token = self.client.cookies.get(auth.COOKIE)
        new_secret = pyotp.random_base32()
        auth.enroll(self.password, new_secret, pyotp.TOTP(new_secret).now(), reset=True)
        self.assertFalse(auth.session_valid(token))
        self.assertEqual(self.login(self.codes[0]).status_code, 401)


if __name__ == '__main__':
    unittest.main()

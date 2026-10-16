"""Adversarial first-install tests against disposable synthetic storage only."""
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pyotp

from app import auth, setup
from app.runtime import RequestBoundaryMiddleware, database_readiness
from app.security import RequestSizeMiddleware, SecurityHeadersMiddleware
from scripts.installation import initialize, setup_token
from scripts.doctor import diagnose


class SetupTests(unittest.TestCase):
    password = 'synthetic setup password for testing'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / 'data' / 'finance.db'
        self.auth_db = self.db.with_name('auth.db')
        self.key = self.root / 'keys' / 'auth.key'
        self.uploads = self.root / 'uploads'
        self.environment = patch.dict(os.environ, {'FINANCE_AUTH_ENABLED': 'true', 'FINANCE_COOKIE_SECURE': 'true',
            'FINANCE_AUTH_DB_PATH': str(self.auth_db), 'FINANCE_AUTH_KEY_PATH': str(self.key),
            'FINANCE_DEPLOYMENT': 'local', 'FINANCE_PUBLIC_ORIGIN': '', 'FINANCE_ALLOWED_HOSTS': 'testserver'})
        self.environment.start()
        self.paths = [patch('app.setup.DB_PATH', self.db), patch('scripts.installation.DB_PATH', self.db),
                      patch('scripts.doctor.DB_PATH', self.db)]
        for override in self.paths:
            override.start()
        self.app = FastAPI()
        self.app.include_router(auth.router)
        self.app.include_router(setup.router)
        self.app.add_middleware(RequestSizeMiddleware)
        self.app.add_middleware(auth.AuthenticationMiddleware)
        self.app.add_middleware(RequestBoundaryMiddleware)
        self.app.add_middleware(SecurityHeadersMiddleware)
        self.client = TestClient(self.app, base_url='https://testserver')

    def tearDown(self):
        self.client.close()
        for override in self.paths:
            override.stop()
        self.environment.stop()
        self.temp.cleanup()

    def initialize(self):
        result = initialize(self.db, self.uploads, self.auth_db, self.key)
        self.token = setup_token()
        return result

    def post(self, path, body, client=None):
        return (client or self.client).post('/api/setup/' + path, json=body, headers={'X-Ledger-Request': '1'})

    def begin(self):
        response = self.post('begin', {'token': self.token})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def enrollment_body(self, challenge):
        return {'token': self.token, 'challenge_id': challenge['challenge_id'], 'password': self.password,
                'code': pyotp.TOTP(challenge['secret']).now()}

    def test_missing_installation_probe_never_creates_storage(self):
        self.assertEqual(self.client.get('/api/setup/status').json(), {'available': False, 'configured': False})
        self.assertEqual(self.post('begin', {'token': 'x' * 43}).status_code, 404)
        self.assertFalse(self.db.exists())
        self.assertFalse(self.auth_db.exists())
        self.assertFalse(self.key.exists())

    def test_initializer_refuses_existing_or_partial_state_and_symlinks(self):
        self.initialize()
        original = self.db.read_bytes(), self.auth_db.read_bytes(), self.key.read_bytes()
        with self.assertRaises(ValueError):
            self.initialize()
        self.assertEqual(original, (self.db.read_bytes(), self.auth_db.read_bytes(), self.key.read_bytes()))
        self.db.unlink()
        with self.assertRaises(ValueError):
            self.initialize()
        self.assertFalse(self.db.exists())
        external = self.root / 'external'; external.mkdir()
        linked = self.root / 'linked'; linked.symlink_to(external)
        with self.assertRaises(ValueError):
            initialize(linked / 'finance.db', self.root / 'new-uploads', linked / 'auth.db', self.root / 'new-keys' / 'auth.key')
        self.assertEqual(list(external.iterdir()), [])

    def test_fresh_storage_ready_private_and_one_time_enrollment(self):
        manifest = self.initialize()
        self.assertEqual(manifest['isolation'], 'one-household-per-instance')
        self.assertEqual(database_readiness(self.db)[1], 200)
        self.assertTrue(diagnose()['ok'])
        self.assertEqual(self.client.get('/api/setup/status').json(), {'available': True, 'configured': False})
        for path in (self.db, self.auth_db, self.key, self.key.with_name('setup-token.txt')):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        challenge = self.begin()
        self.assertIn('otpauth://totp/', challenge['provisioning_uri'])
        with auth.database() as conn:
            stored = conn.execute('SELECT * FROM installation_setup').fetchone()
            self.assertNotEqual(stored['token_digest'], self.token)
            self.assertNotIn(challenge['secret'].encode(), stored['secret'])
        body = self.enrollment_body(challenge)
        response = self.post('enroll', body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()['recovery_codes']), 10)
        self.assertIn('HttpOnly', response.headers['set-cookie'])
        self.assertIn('Secure', response.headers['set-cookie'])
        self.assertTrue(self.client.get('/api/auth/status').json()['authenticated'])
        self.assertEqual(self.client.get('/api/setup/status').json(), {'available': False, 'configured': True})
        self.assertEqual(self.post('enroll', body).status_code, 404)
        self.assertEqual(self.post('begin', {'token': self.token}).status_code, 404)
        with self.assertRaises(ValueError):
            setup_token(renew=True)
        with auth.database() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM installation_setup').fetchone()[0], 0)
        self.assertTrue(diagnose()['owner_configured'])

    def test_existing_unenrolled_installation_is_not_publicly_claimable(self):
        self.initialize()
        with auth.database() as conn:
            conn.execute('DELETE FROM installation_setup'); conn.commit()
        self.assertFalse(self.client.get('/api/setup/status').json()['available'])
        self.assertEqual(self.post('begin', {'token': self.token}).status_code, 404)
        with self.assertRaises(ValueError):
            setup_token(renew=True)

    def test_csrf_origin_body_limit_and_session_boundary(self):
        self.initialize()
        body = {'token': self.token}
        self.assertEqual(self.client.post('/api/setup/begin', json=body).status_code, 403)
        self.assertEqual(self.client.post('/api/setup/begin', json=body,
            headers={'X-Ledger-Request': '1', 'Origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(self.post('begin', {'token': 'x' * 5000}).status_code, 413)
        self.assertEqual(self.client.get('/api/auth/sessions').status_code, 401)
        self.assertEqual(self.client.get('/api/setup/unknown').status_code, 401)

    def test_wrong_tokens_throttled_without_logging_secrets(self):
        self.initialize()
        for _ in range(10):
            self.assertEqual(self.post('begin', {'token': 'x' * 43}).status_code, 401)
        response = self.post('begin', {'token': self.token})
        self.assertEqual(response.status_code, 429)
        self.assertIn('retry-after', response.headers)
        with auth.database() as conn:
            self.assertNotIn(self.token, str([tuple(row) for row in conn.execute('SELECT * FROM security_events')]))
            conn.execute('UPDATE installation_setup SET throttle_until=0'); conn.commit()
        self.begin()

    def test_totp_attempts_cannot_be_reset_by_new_challenges(self):
        self.initialize()
        for _ in range(10):
            challenge = self.begin()
            body = self.enrollment_body(challenge)
            body['code'] = str((int(body['code']) + 1) % 1000000).zfill(6)
            self.assertEqual(self.post('enroll', body).status_code, 401)
        self.assertEqual(self.post('begin', {'token': self.token}).status_code, 429)

    def test_token_expiry_renewal_challenge_expiry_and_missing_key_fail_closed(self):
        self.initialize()
        first_token = self.token
        with auth.database() as conn:
            conn.execute('UPDATE installation_setup SET expires=0'); conn.commit()
        self.assertFalse(self.client.get('/api/setup/status').json()['available'])
        self.assertEqual(self.post('begin', {'token': self.token}).status_code, 404)
        self.token = setup_token(renew=True)
        self.assertNotEqual(first_token, self.token)
        self.assertEqual(self.post('begin', {'token': first_token}).status_code, 401)
        challenge = self.begin()
        with auth.database() as conn:
            conn.execute('UPDATE installation_setup SET challenge_until=0'); conn.commit()
        self.assertEqual(self.post('enroll', self.enrollment_body(challenge)).status_code, 401)
        self.key.unlink()
        self.assertEqual(self.post('begin', {'token': self.token}).status_code, 404)
        self.assertFalse(self.key.exists())
        self.assertFalse(diagnose()['ok'])

    def test_concurrent_enrollment_and_other_device_revocation(self):
        self.initialize()
        body = self.enrollment_body(self.begin())
        def attempt(_):
            with TestClient(self.app, base_url='https://testserver') as client:
                response = self.post('enroll', body, client)
                return response.status_code, response.json(), client.cookies.get(auth.COOKIE)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(sorted(result[0] for result in results), [200, 404])
        winner = next(result for result in results if result[0] == 200)
        codes, original_session = winner[1]['recovery_codes'], winner[2]
        login = self.client.post('/api/auth/login', json={'password': self.password, 'code': codes[0]},
                                 headers={'X-Ledger-Request': '1'})
        self.assertEqual(login.status_code, 200)
        self.assertTrue(auth.session_valid(original_session))
        sessions = self.client.get('/api/auth/sessions').json()['sessions']
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sum(item['current'] for item in sessions), 1)
        self.assertEqual(self.client.post('/api/auth/sessions/revoke-others', headers={'X-Ledger-Request': '1'}).status_code, 200)
        self.assertFalse(auth.session_valid(original_session))
        self.assertTrue(self.client.get('/api/auth/status').json()['authenticated'])

    def test_terminal_recovery_cannot_replace_a_missing_existing_key(self):
        self.initialize()
        challenge = self.begin()
        self.assertEqual(self.post('enroll', self.enrollment_body(challenge)).status_code, 200)
        with auth.database() as conn:
            original_owner = tuple(conn.execute('SELECT * FROM owner').fetchone())
        self.key.unlink()
        secret = pyotp.random_base32()
        with self.assertRaisesRegex(ValueError, 'matching key'):
            auth.enroll(self.password, secret, pyotp.TOTP(secret).now(), reset=True)
        self.assertFalse(self.key.exists())
        with auth.database() as conn:
            self.assertEqual(tuple(conn.execute('SELECT * FROM owner').fetchone()), original_owner)


if __name__ == '__main__':
    unittest.main()

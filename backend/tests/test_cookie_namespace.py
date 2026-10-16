"""One browser must retain independent sessions for households on different ports."""
from contextlib import contextmanager
from http.cookiejar import CookieJar
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import pyotp

from app import auth
from app.main import app


class HouseholdCookieTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='ledger-cookie-')))
        self.enterContext(patch.dict(os.environ, {'FINANCE_AUTH_ENABLED': 'true',
            'FINANCE_DEPLOYMENT': 'local', 'FINANCE_PUBLIC_ORIGIN': '',
            'FINANCE_ALLOWED_HOSTS': 'localhost,testserver'}))

    def test_legacy_cookie_names_are_unchanged_when_instance_id_is_absent_or_empty(self):
        for secure, expected in (('true', '__Host-ledger_session'), ('false', 'ledger_session')):
            with self.subTest(secure=secure), patch.dict(os.environ, {'FINANCE_COOKIE_SECURE': secure}):
                with patch.dict(os.environ):
                    os.environ.pop('FINANCE_INSTANCE_ID', None)
                    self.assertEqual(auth.cookie_name(), expected)
                with patch.dict(os.environ, {'FINANCE_INSTANCE_ID': ''}):
                    self.assertEqual(auth.cookie_name(), expected)

    def test_namespace_is_stable_distinct_and_safe_for_arbitrary_instance_text(self):
        names = []
        for instance in ('ledger-household-a', 'ledger-household-b', 'odd/instance;\r\nname=💰' * 100):
            with patch.dict(os.environ, {'FINANCE_COOKIE_SECURE': 'true', 'FINANCE_INSTANCE_ID': instance}):
                name = auth.cookie_name()
                self.assertEqual(auth.cookie_name(), name)
                self.assertRegex(name, r'^__Host-ledger_session_[a-f0-9]{16}$')
                names.append(name)
        self.assertEqual(len(set(names)), len(names))

    @contextmanager
    def household(self, name, secure):
        root = self.directory / (secure + '-' + name)
        with patch.dict(os.environ, {'FINANCE_COOKIE_SECURE': secure, 'FINANCE_INSTANCE_ID': 'ledger-' + name,
            'FINANCE_AUTH_DB_PATH': str(root / 'auth.db'), 'FINANCE_AUTH_KEY_PATH': str(root / 'auth.key')}):
            yield

    def sign_in(self, client):
        secret = pyotp.random_base32()
        password = 'Synthetic independent household password'
        codes = auth.enroll(password, secret, pyotp.TOTP(secret).now())
        response = client.post('/api/auth/login', json={'password': password, 'code': codes[0]},
                               headers={'X-Ledger-Request': '1'})
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def test_shared_browser_cookie_jar_keeps_both_households_signed_in_and_logs_out_only_one(self):
        for secure, scheme in (('false', 'http'), ('true', 'https')):
            with self.subTest(secure=secure):
                jar = CookieJar()
                first = TestClient(app, base_url=scheme + '://localhost:21001')
                second = TestClient(app, base_url=scheme + '://localhost:21002')
                self.addCleanup(first.close)
                self.addCleanup(second.close)
                first.cookies.jar = jar
                second.cookies.jar = jar
                with self.household('alpha', secure):
                    response = self.sign_in(first)
                    first_name = auth.cookie_name()
                    first_token = first.cookies.get(first_name)
                    for flag in ('HttpOnly', 'SameSite=strict', 'Path=/'):
                        self.assertIn(flag, response.headers['set-cookie'])
                    self.assertEqual('Secure' in response.headers['set-cookie'], secure == 'true')
                with self.household('beta', secure):
                    # The jar really sends the first household's cookie across
                    # ports. It must not authenticate to a different household.
                    self.assertIn(first_name + '=', second.build_request('GET', '/api/auth/status').headers['cookie'])
                    self.assertFalse(second.get('/api/auth/status').json()['authenticated'])
                    self.sign_in(second)
                    second_name = auth.cookie_name()
                    second_token = second.cookies.get(second_name)
                    self.assertTrue(second.get('/api/auth/status').json()['authenticated'])
                    self.assertFalse(auth.session_valid(first_token))
                self.assertEqual({cookie.name for cookie in jar}, {first_name, second_name})
                with self.household('alpha', secure):
                    self.assertTrue(first.get('/api/auth/status').json()['authenticated'])
                    self.assertFalse(auth.session_valid(second_token))
                    response = first.post('/api/auth/logout', headers={'X-Ledger-Request': '1'})
                    self.assertEqual(response.status_code, 200)
                    self.assertFalse(first.get('/api/auth/status').json()['authenticated'])
                    self.assertFalse(auth.session_valid(first_token))
                self.assertEqual({cookie.name for cookie in jar}, {second_name})
                with self.household('beta', secure):
                    self.assertTrue(second.get('/api/auth/status').json()['authenticated'])
                    self.assertTrue(auth.session_valid(second_token))
                    self.assertEqual(second.get('/api/auth/sessions').status_code, 200)


if __name__ == '__main__':
    unittest.main()

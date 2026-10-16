import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app.security import RequestSizeMiddleware, validate_deployment
from app.network import urlopen, NoRedirects
from app import main


class RequestSecurityTests(unittest.TestCase):
    def test_streamed_body_over_limit_is_rejected_before_application(self):
        called = False
        async def endpoint(scope, receive, send):
            nonlocal called
            called = True
        messages = iter([{'type': 'http.request', 'body': b'x' * 2048, 'more_body': True},
                         {'type': 'http.request', 'body': b'y' * 2049, 'more_body': False}])
        sent = []
        async def receive():
            return next(messages)
        async def send(message):
            sent.append(message)
        asyncio.run(RequestSizeMiddleware(endpoint)(
            {'type': 'http', 'path': '/api/auth/login', 'method': 'POST', 'headers': []}, receive, send))
        self.assertFalse(called)
        self.assertEqual(sent[0]['status'], 413)

    def test_auth_payload_cannot_echo_secrets(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'FINANCE_AUTH_ENABLED': 'false'}), patch.object(main, 'DB_PATH', Path(directory) / 'unused.db'):
            with TestClient(main.app) as client:
                secret = 'SECRET-' * 40
                result = client.post('/api/auth/login', json={'password': secret, 'code': '123456'})
                self.assertEqual(result.status_code, 422)
                self.assertNotIn(secret, result.text)
                self.assertNotIn('input', result.text)
                self.assertIn('content-security-policy', result.headers)
                self.assertEqual(client.post('/api/auth/login', content=b'x' * 4097).status_code, 413)
                self.assertEqual(client.post('/api/auth/login', content=b'x', headers={'Content-Encoding': 'gzip'}).status_code, 415)
                rejection = client.get('/api/summary', headers={'Host': 'evil.example'})
                self.assertEqual(rejection.status_code, 400)
                self.assertEqual(rejection.headers['cache-control'], 'no-store')

    def test_public_deployment_configuration_fails_closed(self):
        base = {'FINANCE_DEPLOYMENT': 'public', 'FINANCE_AUTH_ENABLED': 'true',
                'FINANCE_COOKIE_SECURE': 'true', 'FINANCE_PUBLIC_ORIGIN': 'https://ledger.example.com',
                'FINANCE_ALLOWED_HOSTS': 'ledger.example.com', 'FINANCE_AUTH_DB_PATH': '/app/data/auth.db', 'FINANCE_AUTH_KEY_PATH': '/app/keys/auth.key'}
        with patch.dict(os.environ, base):
            validate_deployment()
            for key, value in [('FINANCE_AUTH_ENABLED', 'false'), ('FINANCE_COOKIE_SECURE', 'false'),
                               ('FINANCE_PUBLIC_ORIGIN', 'http://ledger.example.com'),
                               ('FINANCE_ALLOWED_HOSTS', 'different.example.com'),
                               ('FINANCE_AUTH_KEY_PATH', '/app/data/auth.key'), ('FINANCE_AUTH_KEY_PATH', '')]:
                with self.subTest(key=key), patch.dict(os.environ, {key: value}):
                    with self.assertRaises(RuntimeError):
                        validate_deployment()

    def test_unrecognized_deployment_mode_cannot_bypass_public_guards(self):
        with patch.dict(os.environ, {'FINANCE_DEPLOYMENT': 'publc',
                                     'FINANCE_AUTH_ENABLED': 'false',
                                     'FINANCE_COOKIE_SECURE': 'false',
                                     'FINANCE_PUBLIC_ORIGIN': 'http://ledger.example.com'}):
            with self.assertRaisesRegex(RuntimeError, 'FINANCE_DEPLOYMENT'):
                validate_deployment()

    def test_public_network_requests_cannot_target_local_hosts_or_follow_redirects(self):
        with patch('app.network.build_opener') as opener:
            for url in ['http://www.wikidata.org/a', 'https://127.0.0.1/', 'https://169.254.169.254/',
                        'https://www.wikidata.org.evil.example/', 'file:///etc/passwd',
                        'https://user:password@www.wikidata.org/', 'https://www.wikidata.org:444/']:
                with self.assertRaises(ValueError):
                    urlopen(url)
            opener.assert_not_called()
            urlopen('https://www.wikidata.org/w/api.php')
            opener.return_value.open.assert_called_once()
        self.assertIsNone(NoRedirects().redirect_request(None, None, 302, '', {}, 'http://127.0.0.1/'))

    def test_retired_upload_has_no_large_body_exemption(self):
        async def endpoint(scope, receive, send):
            self.fail('Oversized retired upload reached application')
        async def receive():
            self.fail('Known oversized body was buffered')
        sent = []
        async def send(message):
            sent.append(message)
        asyncio.run(RequestSizeMiddleware(endpoint)(
            {'type': 'http', 'path': '/api/upload', 'method': 'POST',
             'headers': [(b'content-length', str(1024 * 1024 + 1).encode())]}, receive, send))
        self.assertEqual(sent[0]['status'], 413)



if __name__ == '__main__':
    unittest.main()

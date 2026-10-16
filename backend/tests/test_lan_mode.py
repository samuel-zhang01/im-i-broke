import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import main
from app.db import init_db
from app.security import validate_deployment


class TrustedLANTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'finance.db'
        init_db(self.path).close()
        self.env = patch.dict(os.environ, {
            'FINANCE_DEPLOYMENT': 'trusted-lan', 'FINANCE_AUTH_ENABLED': 'false',
            'FINANCE_BIND_ADDRESS': '192.168.50.10', 'FINANCE_PUBLIC_ORIGIN': '',
            'FINANCE_AUTH_DB_PATH': str(Path(self.temp.name) / 'auth.db'),
        })
        self.env.start()
        self.db = patch.object(main, 'DB_PATH', self.path); self.db.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close(); self.db.stop(); self.env.stop(); self.temp.cleanup()

    def test_no_enrollment_cookie_or_login_required(self):
        status = self.client.get('/api/auth/status')
        self.assertEqual(status.json(), {'enabled': False, 'configured': False, 'authenticated': True})
        self.assertNotIn('set-cookie', status.headers)
        self.assertEqual(self.client.get('/api/summary').status_code, 200)
        self.assertFalse((Path(self.temp.name) / 'auth.db').exists())
        self.assertEqual(self.client.post('/api/auth/login', headers={'X-Ledger-Request': '1'}, json={}).status_code, 404)

    def test_same_origin_ui_works_but_cross_origin_writes_are_denied(self):
        payload = {'name': 'LAN test', 'parameters': {}}
        self.assertEqual(self.client.post('/api/scenarios', json=payload).status_code, 403)
        result = self.client.post('/api/scenarios', json=payload, headers={'X-Ledger-Request': '1', 'Origin': 'http://testserver'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.client.post('/api/scenarios', json=payload, headers={
            'X-Ledger-Request': '1', 'Origin': 'https://untrusted.example'}).status_code, 403)
        self.assertEqual(self.client.get('/api/summary', headers={'Sec-Fetch-Site': 'cross-site'}).status_code, 403)
        self.assertNotIn('access-control-allow-origin', result.headers)

    def test_unsafe_binding_or_public_origin_rejected(self):
        validate_deployment()
        for address in ('0.0.0.0', '::', '8.8.8.8'):
            with patch.dict(os.environ, {'FINANCE_BIND_ADDRESS': address}):
                with self.assertRaises(RuntimeError): validate_deployment()
        with patch.dict(os.environ, {'FINANCE_PUBLIC_ORIGIN': 'https://ledger.example.com'}):
            with self.assertRaises(RuntimeError): validate_deployment()
        with patch.dict(os.environ, {'FINANCE_DEPLOYMENT': 'public'}):
            with self.assertRaises(RuntimeError): validate_deployment()

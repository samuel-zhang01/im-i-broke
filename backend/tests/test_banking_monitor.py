"""Request integration: authentication, read-only health and private timing."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import banking_health, lunchflow_routes, lunchflow_store
from app.banking_monitor import BankingTimingMiddleware
from app.db import init_db
from app.main import app


class BankingMonitorTests(unittest.TestCase):
    def test_middleware_aggregates_account_routes_and_ignores_investments(self):
        async def exercise():
            async def application(scope, receive, send):
                await send({'type': 'http.response.start', 'status': 404, 'headers': []})
                await send({'type': 'http.response.body', 'body': b'private body'})
            async def send(message):
                pass
            async def receive():
                return {'type': 'http.request'}
            monitor = BankingTimingMiddleware(application)
            await monitor({'type': 'http', 'path': '/api/banking/accounts/private-id',
                           'query_string': b'q=private-name'}, receive, send)
            await monitor({'type': 'http', 'path': '/api/investments'}, receive, send)
        with patch.object(banking_health, '_timings', {}):
            asyncio.run(exercise())
            result = banking_health.timing_snapshot()
            self.assertEqual(list(result['operations']), ['account_detail'])
            self.assertEqual(result['operations']['account_detail']['client_errors'], 1)
            self.assertNotIn('private', json.dumps(result))

    def test_health_is_authenticated_and_a_no_store_read_without_provider_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'finance.db'
            conn = init_db(path)
            lunchflow_store.ensure_schema(conn)
            conn.commit()
            conn.close()
            with patch.dict(os.environ, {'FINANCE_AUTH_ENABLED': 'true',
                    'FINANCE_AUTH_DB_PATH': str(Path(folder) / 'auth.db'),
                    'FINANCE_AUTH_KEY_PATH': str(Path(folder) / 'auth.key')}):
                client = TestClient(app, base_url='https://testserver')
                try:
                    with patch.object(lunchflow_routes, '_open') as opened:
                        self.assertEqual(client.get('/api/banking/health').status_code, 401)
                        opened.assert_not_called()
                finally:
                    client.close()

            def read_only_connection():
                result = lunchflow_store.database(path)
                result.execute('PRAGMA query_only=ON')
                return result
            with patch.dict(os.environ, {'FINANCE_AUTH_ENABLED': 'false'}), \
                    patch.object(lunchflow_routes, '_open', side_effect=read_only_connection), \
                    patch('app.lunchflow_client.read', side_effect=AssertionError('No provider calls')):
                # No lifespan: this test concerns only the authenticated route.
                client = TestClient(app, base_url='http://testserver')
                response = client.get('/api/banking/health')
                client.close()
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['cache-control'], 'no-store')
                self.assertTrue(response.json()['read_only'])
                self.assertTrue(response.json()['schema_ready'])


if __name__ == '__main__':
    unittest.main()

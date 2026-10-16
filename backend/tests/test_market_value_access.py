"""Market-value reads retain the existing auth wall and never refresh providers."""
from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import db, main, snaptrade_store
from app.market_cache import RefreshBusy


class MarketValueAccessTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'finance.db'
        db.init_db(self.path).close()
        now = datetime.now(timezone.utc).isoformat()
        snaptrade_store.save_portfolio({'configured':True, 'retrieved_at':now, 'accounts':[
            {'id':'test-cash', 'name':'Synthetic cash account', 'institution':'Synthetic broker',
             'currency':'GBP', 'total_value':20, 'holdings_synced_at':now,
             'initial_sync_complete':True,
             'positions':{'status':'empty','data':[]},
             'balances':{'status':'ok','data':[{'currency':'GBP','cash':20}]}}],
            'connections':{'status':'empty','data':[]}}, path=self.path)
        for context in (
            patch.dict(os.environ, {'FINANCE_AUTH_ENABLED':'true','FINANCE_DEPLOYMENT':'local',
                                   'INVESTMENT_PRICE_REFRESH_SECONDS':'0'}),
            patch.object(db,'DB_PATH',self.path),
            patch.object(main,'DB_PATH',self.path),
        ):
            context.start()
            self.addCleanup(context.stop)
        # No lifespan: the test is an HTTP read, never a background sync job.
        self.client = TestClient(main.app,base_url='https://testserver')
        self.addCleanup(self.client.close)

    def contents(self):
        with closing(sqlite3.connect('file:' + str(self.path) + '?mode=ro',uri=True)) as connection, connection:
            return '\n'.join(connection.iterdump())

    def test_market_value_requires_auth_before_reading_private_portfolio(self):
        with patch('app.auth.session_valid',return_value=False), \
                patch('app.market_valuation.bundle') as bundle, \
                patch('app.intraday_market.refresh_intraday') as refresh:
            result = self.client.get('/api/investments/market/value')
        self.assertEqual(result.status_code,401)
        self.assertEqual(result.headers['cache-control'],'no-store')
        bundle.assert_not_called()
        refresh.assert_not_called()

    def test_authenticated_get_uses_saved_data_without_provider_calls_or_financial_writes(self):
        before = self.contents()
        with patch('app.auth.session_valid',return_value=True), \
                patch('app.intraday_market.refresh_intraday') as hourly, \
                patch('app.market_cache.refresh_market_data') as daily, \
                patch('app.snaptrade_client.call') as broker, \
                patch('app.prices._get_json') as public:
            first = self.client.get('/api/investments/market/value?range=1d&interval=1h&account=test-cash')
            second = self.client.get('/api/investments/market/value?range=1d&interval=1h&account=test-cash')
        for result in (first,second):
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(result.headers['cache-control'],'no-store')
            self.assertEqual(result.json()['basis'],'current_holdings')
            self.assertEqual(result.json()['summary']['value'],20)
            self.assertEqual(result.json()['selected_account_ids'],['test-cash'])
        self.assertEqual(self.contents(),before)
        for mock in (hourly,daily,broker,public):
            mock.assert_not_called()

    def test_invalid_range_and_unknown_account_are_rejected_without_refresh(self):
        before = self.contents()
        with patch('app.auth.session_valid',return_value=True), \
                patch('app.intraday_market.refresh_intraday') as refresh:
            for query in ('range=2y','interval=5m','account=missing'):
                with self.subTest(query=query):
                    result = self.client.get('/api/investments/market/value?' + query)
                    self.assertEqual(result.status_code,422,result.text)
        self.assertEqual(self.contents(),before)
        refresh.assert_not_called()

    def test_hourly_refresh_busy_preserves_successful_daily_refresh_response(self):
        with patch('app.auth.session_valid',return_value=True), \
                patch('app.market_routes.refresh_market_data',return_value={
                    'updated':3,'failed':0,'retrieved_at':'2026-09-18T15:40:00Z',
                }) as daily, \
                patch('app.intraday_market.refresh_intraday',side_effect=RefreshBusy('Already refreshing')) as hourly:
            result = self.client.post('/api/investments/market/refresh',headers={'X-Ledger-Request':'1'})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.headers['cache-control'],'no-store')
        self.assertEqual(result.json()['updated'],3)
        self.assertEqual(result.json()['failed'],0)
        self.assertEqual(result.json()['retrieved_at'],'2026-09-18T15:40:00Z')
        self.assertEqual(result.json()['intraday']['status'],'busy')
        self.assertIn('saved cache',result.json()['intraday']['message'])
        daily.assert_called_once_with(include_benchmarks=True)
        hourly.assert_called_once_with()

    def test_busy_daily_refresh_still_returns_rate_limit_without_starting_hourly_refresh(self):
        with patch('app.auth.session_valid',return_value=True), \
                patch('app.market_routes.refresh_market_data',side_effect=RefreshBusy('Daily refresh is busy')) as daily, \
                patch('app.intraday_market.refresh_intraday') as hourly:
            result = self.client.post('/api/investments/market/refresh',headers={'X-Ledger-Request':'1'})
        self.assertEqual(result.status_code,429,result.text)
        self.assertEqual(result.headers['retry-after'],'60')
        self.assertEqual(result.headers['cache-control'],'no-store')
        self.assertEqual(result.json()['detail'],'Daily refresh is busy')
        daily.assert_called_once_with(include_benchmarks=True)
        hourly.assert_not_called()


if __name__ == '__main__':
    unittest.main()

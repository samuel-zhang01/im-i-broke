"""Large cached investment reads remain bounded and usable through HTTP proxies."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import auth, db, main, market_cache, product_connections, snaptrade_models, snaptrade_store
from app.account_selection import MAX_SELECTED_ACCOUNTS


class LargeAccountSelectionTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(folder) / 'synthetic.db'
        self.enterContext(patch.object(db, 'DB_PATH', self.path))
        self.enterContext(patch.object(main, 'DB_PATH', self.path))
        self.enterContext(patch.object(auth, 'enabled', return_value=True))
        self.enterContext(patch.object(auth, 'session_valid', return_value=True))
        self.conn = db.init_db(self.path)
        self.addCleanup(self.conn.close)
        product_connections.ensure_schema(self.conn)
        self.conn.executescript(market_cache.SCHEMA)
        self.conn.commit()
        self.ids = []
        stamp = datetime.now(timezone.utc).isoformat()
        for index in range(3):
            stamp = datetime.now(timezone.utc).isoformat()
            scope = f'connection:{index:032x}'
            self.conn.execute("INSERT INTO provider_connections(id,provider,label,status,created_at,updated_at) VALUES(?,'snaptrade','Synthetic','ready',?,?)", (scope,stamp,stamp))
            self.conn.commit()
            rows = []
            for offset in range(30):
                ident = f'{scope}:{index * 30 + offset:08x}-1111-2222-3333-444444444444'
                self.ids.append(ident)
                row = snaptrade_models.account({'id': ident, 'name': 'Synthetic account', 'institution_name': 'Example',
                    'balance': {'total': {'amount': 10, 'currency': 'GBP'}}})
                row['positions'] = snaptrade_models.section([])
                row['balances'] = snaptrade_models.section([{'currency':'GBP','cash':10}])
                rows.append(row)
            snaptrade_store.save_portfolio({'configured': True, 'retrieved_at': stamp, 'accounts': rows,
                'connections': snaptrade_models.section([])}, path=self.path, connection_scope=scope)
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        self.headers = {'X-Ledger-Request': '1'}

    def test_large_reads_share_get_cache_preserve_filters_and_never_write(self):
        endpoints = [
            ('/api/wealth', {'currency':'GBP','range':'1m'}),
            ('/api/investments/analysis', {'currency':'GBP','range':'1w'}),
            ('/api/investments/analysis/calendar', {'currency':'GBP','year':2026,'month':8,'source':'recorded'}),
            ('/api/investments/analysis/insights', {'currency':'GBP'}),
            ('/api/investments/analysis/exposure', {'currency':'GBP'}),
            ('/api/investments/analysis/coverage', {'currency':'GBP'}),
            ('/api/investments/market/value', {'currency':'GBP','range':'1w','interval':'1d'}),
        ]
        before = '\n'.join(self.conn.iterdump())
        for endpoint, filters in endpoints:
            with self.subTest(endpoint=endpoint):
                selection = ['connected:' + ident for ident in self.ids] if endpoint == '/api/wealth' else self.ids
                posted = self.client.post(endpoint, params=filters, json={'account':selection}, headers=self.headers)
                self.assertEqual(posted.status_code, 200, posted.text[:300])
                self.assertEqual(posted.json()['selected_account_ids'], selection)
                get = self.client.get(endpoint, params=list(filters.items())+[('account',ident) for ident in selection])
                self.assertEqual(get.status_code, 200, get.text[:300])
                self.assertEqual(get.content, posted.content)
                self.assertEqual(get.headers['X-Ledger-Investment-Cache'], 'HIT')
                self.assertEqual(posted.headers['Cache-Control'], 'no-store')
                for key in ('currency','range','year','month','source','interval'):
                    if key in filters and key in posted.json():
                        self.assertEqual(posted.json()[key], filters[key])
        self.assertEqual('\n'.join(self.conn.iterdump()), before)

    def test_wealth_body_preserves_long_namespaced_source_ids(self):
        account = 'synthetic-' + 'x' * 276
        today = datetime.now(timezone.utc).date().isoformat()
        with self.conn:
            self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES(?,'Example','cash','Long source identity')", (account,))
            self.conn.execute("INSERT INTO statements(account_id,period_start,period_end,closing_balance,reconciled,source_file) VALUES(?,?,?,?,1,'synthetic-long-source')", (account,today,today,100))
        selected = 'statement:' + account
        self.assertGreater(len(selected), 240)
        get = self.client.get('/api/wealth', params={'account':selected})
        post = self.client.post('/api/wealth', json={'account':[selected]}, headers=self.headers)
        self.assertEqual(get.status_code,200,get.text[:300])
        self.assertEqual(post.status_code,200,post.text[:300])
        self.assertEqual(post.content,get.content)
        self.assertEqual(post.json()['summary']['net_worth'],100)
        self.assertEqual(self.client.post('/api/wealth',json={'account':['x'*301]},headers=self.headers).status_code,422)

    def test_selection_body_is_bounded_explicit_and_protected(self):
        url = '/api/investments/analysis'
        for body in ({}, {'account':None}, {'account':[1]}, {'account':['x'*241]},
                     {'account':self.ids,'refresh':True}, {'account':[self.ids[0]]*(MAX_SELECTED_ACCOUNTS+1)},
                     {'account':['unknown']}):
            with self.subTest(body_shape=list(body)):
                response = self.client.post(url, json=body, headers=self.headers)
                self.assertEqual(response.status_code, 422, response.text[:300])
        self.assertEqual(self.client.post(url,json={'account':self.ids}).status_code,403)
        with patch.object(auth,'session_valid',return_value=False):
            self.assertEqual(self.client.post(url,json={'account':self.ids},headers=self.headers).status_code,401)
        for selection in ([], [self.ids[0]], [self.ids[0], self.ids[0]]):
            response=self.client.post(url,json={'account':selection},headers=self.headers)
            self.assertEqual(response.status_code,200,response.text[:300])
            self.assertEqual(response.json()['selected_account_ids'],list(dict.fromkeys(selection)))


if __name__ == '__main__':
    unittest.main()

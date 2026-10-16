"""Provider combinations use disposable households and synthetic public identities."""
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from app import account_settings, db, investing_analytics, lunchflow_client, lunchflow_routes, lunchflow_store
from app import product_connections, snaptrade_client, snaptrade_models, snaptrade_routes, snaptrade_store, wealth


class ProviderPortabilityTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'finance.db'
        key = Path(folder.name) / 'key'; key.write_bytes(Fernet.generate_key())
        for context in (patch.object(db, 'DB_PATH', self.path),
                        patch.dict(os.environ, {'FINANCE_AUTH_KEY_PATH': str(key), 'LUNCH_FLOW_API': '', 'SNAPTRADE_ID': '', 'SNAPTRADE_KEY': ''}),
                        patch.object(snaptrade_routes, '_next_read', 0)):
            context.start(); self.addCleanup(context.stop)
        self.conn = db.init_db(self.path); self.addCleanup(self.conn.close)
        lunchflow_store.ensure_schema(self.conn); product_connections.ensure_schema(self.conn); self.conn.commit()

    def payload(self, names=('Example Broker',), currencies=('GBP',)):
        stamp = datetime.now(timezone.utc).isoformat()
        accounts = []
        for index, (name, currency) in enumerate(zip(names, currencies)):
            row = snaptrade_models.account({'id': 'synthetic-' + str(index), 'name': 'Everyday',
                'institution_name': name, 'brokerage_authorization': 'authorization',
                'balance': {'total': {'amount': 100, 'currency': currency}},
                'sync_status': {'holdings': {'initial_sync_completed': True, 'last_successful_sync': stamp},
                                'transactions': {'initial_sync_completed': False}}})
            row['balances'] = snaptrade_models.section([{'currency': currency, 'cash': 100}])
            row['positions'] = {**snaptrade_models.section([]), 'as_of': stamp}
            row['activities'] = snaptrade_models.section(unsupported=True, error='History unavailable')
            accounts.append(row)
        return {'configured': True, 'retrieved_at': stamp, 'error': None, 'accounts': accounts,
                'connections': snaptrade_models.section([])}

    def create(self, provider='snaptrade', suffix='one'):
        credentials = {'client_id': 'synthetic-' + suffix, 'consumer_key': 'synthetic-secret-' + suffix} if provider == 'snaptrade' else {'api_key': 'synthetic-' + suffix}
        with self.conn:
            return product_connections.create_connection(self.conn, product_connections.ConnectionCreate(
                provider=provider, label='Example ' + suffix, credentials=credentials))

    def test_non_latin_banks_never_share_an_empty_identity_key(self):
        names = ('中国银行', '中国建设银行', 'بنك المثال', 'Банк Пример')
        self.assertEqual(len({lunchflow_store.bank_key(name) for name in names}), len(names))
        accounts = [account_settings.create_manual(self.conn, account_settings.ManualAccount(display_name='Everyday', institution=name)) for name in names]
        raw = lunchflow_client.list_accounts({'accounts': [{'id': 'discovered', 'name': 'Everyday', 'institution_name': names[0], 'currency': 'GBP'}]})
        lunchflow_store.discover_accounts(self.conn, raw, lunchflow_store.now())
        candidates = lunchflow_store.candidates(self.conn, dict(self.conn.execute('SELECT * FROM bank_connections').fetchone()))
        self.assertEqual([a['id'] for a in candidates], [accounts[0]['id']])

    def test_unlinked_new_bank_is_visible_as_a_setup_gap_in_default_net_worth(self):
        discovered = lunchflow_client.list_accounts({'accounts': [{'id': 'new-bank', 'name': 'Everyday', 'institution_name': 'Future Bank', 'currency': 'GBP'}]})
        lunchflow_store.discover_accounts(self.conn, discovered, lunchflow_store.now()); self.conn.commit()
        result = wealth.bundle(self.conn, path=self.path)
        self.assertEqual(result['summary']['unmapped_bank_accounts'], 1)
        self.assertTrue(result['summary']['partial'])
        self.assertTrue(any('need linking' in message for message in result['warnings']))
        # An explicit investments-only selection is not qualified by unrelated banks.
        self.assertEqual(wealth.bundle(self.conn, path=self.path, account_ids=[])['summary']['unmapped_bank_accounts'], 0)
        with self.conn:
            lunchflow_store.map_account(self.conn, 'new-bank', create_new=True, account_type='current')
        self.assertEqual(wealth.bundle(self.conn, path=self.path)['summary']['unmapped_bank_accounts'], 0)

    def test_different_institutions_and_currencies_keep_current_values_without_activity_support(self):
        names = ('New Canadian Broker', '新证券', 'Example Retirement', 'Future Crypto Exchange', 'European Broker')
        codes = ('CAD', 'JPY', 'GBP', 'USD', 'EUR')
        payload = self.payload(names, codes)
        snaptrade_store.save_portfolio(payload, self.path)
        for code in codes:
            if code != 'GBP':
                self.conn.execute('INSERT INTO reference_fx VALUES(?,?,?,?,?,?,?)',
                    (date.today().isoformat(), code, .5, 'synthetic', date.today().isoformat(), payload['retrieved_at'], 'https://example.test/fx'))
        self.conn.commit()
        result = investing_analytics.bundle(self.conn, path=self.path)
        self.assertEqual(result['summary']['value'], 300)
        self.assertEqual(result['summary']['known_accounts'], 5)
        self.assertFalse(result['summary']['partial'])
        self.assertTrue(all(not a['stale'] for a in result['accounts']))
        self.assertEqual({a['institution'] for a in result['accounts']}, set(names))

    def test_optional_activity_failure_does_not_mark_other_account_balances_stale(self):
        payload = self.payload(('First Broker', 'Second Broker'), ('GBP', 'GBP'))
        payload['accounts'][0]['activities'] = snaptrade_models.section(error='History unavailable')
        snaptrade_store.save_portfolio(payload, self.path)
        cached = snaptrade_store.load_portfolio(self.path)
        self.assertEqual(cached['accounts'][0]['activities']['status'], 'error')

        self.assertFalse(any(a['connection_stale'] for a in cached['accounts']))
        self.assertFalse(wealth.bundle(self.conn, path=self.path)['summary']['partial'])

    def test_server_broker_failure_does_not_prevent_saved_connection_refresh(self):
        saved = self.create(); calls = []
        with patch.object(snaptrade_routes, 'configured', return_value=True), \
                patch.object(product_connections, 'refresh_saved_connections', side_effect=lambda *a: calls.append(a) or []), \
                patch.object(snaptrade_routes, 'fetch_portfolio', return_value={'error': 'Synthetic provider outage', 'accounts': []}):
            response = snaptrade_routes.refresh_portfolio()
        self.assertEqual(calls, [('snaptrade',)])
        self.assertEqual(response.status_code, 502)
        self.assertEqual(product_connections.sync_statuses('snaptrade')[0]['id'], saved['id'])

    def test_server_bank_busy_does_not_prevent_saved_connection_refresh(self):
        self.create('lunchflow'); calls = []
        with patch.object(lunchflow_client, 'configured', return_value=True), \
                patch.object(product_connections, 'refresh_saved_connections', side_effect=lambda *a: calls.append(a) or []), \
                patch.object(lunchflow_routes, 'run_sync', side_effect=ValueError('A sync is running')):
            response = lunchflow_routes.sync_banking()
        self.assertEqual(calls, [('lunchflow',)])
        self.assertEqual(response.status_code, 429)

    def test_failed_first_saved_connection_is_reported_without_a_cache(self):
        saved = self.create()
        with self.conn:
            self.conn.execute("UPDATE provider_connections SET status='error',last_error='Synthetic unavailable' WHERE id=?", (saved['id'],))
        response = json.loads(snaptrade_routes.portfolio_response(self.payload()).body)
        self.assertEqual(response['connection_statuses'][0]['last_error'], 'Synthetic unavailable')
        self.assertIsNone(response['connection_statuses'][0]['last_synced_at'])
        self.assertNotIn('secrets', json.dumps(response))

    def test_an_unimplemented_connector_cannot_accidentally_use_snaptrade_credentials(self):
        adapter = product_connections.ProviderAdapter('future-provider', 'Future', (), ())
        with patch.object(snaptrade_client, 'call') as call:
            with self.assertRaisesRegex(ValueError, 'read adapter'): adapter.reader({})
            with self.assertRaisesRegex(ValueError, 'sync adapter'): adapter.sync({}, 'new', self.path)
        call.assert_not_called()

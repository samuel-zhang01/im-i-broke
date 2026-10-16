"""Connection races, credential recovery and aggregate reads with synthetic storage."""
from datetime import date, datetime, timezone
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db, product_connections as connections, snaptrade_models, snaptrade_store
from app import investing_analytics, investing_calendar, investment_coverage, market_valuation
from app import snaptrade_routes, investing_routes


class ConnectionAuditTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'finance.db'
        self.key = Path(folder.name) / 'auth.key'
        self.key.write_bytes(Fernet.generate_key())
        for context in (patch.object(db, 'DB_PATH', self.path),
                        patch.dict(os.environ, {'FINANCE_AUTH_KEY_PATH': str(self.key), 'LUNCH_FLOW_API': '',
                                               'SNAPTRADE_ID': '', 'SNAPTRADE_KEY': ''})):
            context.start()
            self.addCleanup(context.stop)
        self.conn = db.init_db(self.path)
        self.addCleanup(self.conn.close)
        connections.ensure_schema(self.conn)
        self.conn.commit()

    def body(self, suffix='one'):
        return connections.ConnectionCreate(provider='snaptrade', label='Synthetic broker ' + suffix,
            credentials={'client_id': 'synthetic-client-' + suffix, 'consumer_key': 'synthetic-key-' + suffix})

    def create(self, suffix='one'):
        with self.conn:
            return connections.create_connection(self.conn, self.body(suffix))

    def save_cache(self, saved, count=1, suffix='one'):
        rows = []
        for index in range(count):
            row = snaptrade_models.account({'id': saved['id'] + ':' + suffix + '-' + str(index),
                'name': 'Synthetic account', 'institution_name': 'Example brokerage',
                'balance': {'total': {'amount': 10, 'currency': 'GBP'}}})
            row['positions'] = snaptrade_models.section([])
            row['balances'] = snaptrade_models.section([{'currency': 'GBP', 'cash': 10}])
            rows.append(row)
        minute = self.conn.execute('SELECT COUNT(*) FROM provider_connections').fetchone()[0]
        stamp = f'2026-09-20T12:{minute:02d}:00+00:00'
        snaptrade_store.save_portfolio({'configured': True, 'retrieved_at': stamp,
            'accounts': rows, 'connections': snaptrade_models.section([])}, path=self.path, connection_scope=saved['id'])
        with self.conn:
            self.conn.execute("UPDATE provider_connections SET status='ready' WHERE id=?", (saved['id'],))

    def interleave_create(self, first_body, second_body, *, deferred=False):
        """A second SQLite writer cannot pass validation while the first pauses."""
        second = db.connect(self.path)
        self.addCleanup(second.close)
        second.execute('PRAGMA busy_timeout=0')
        original = connections._assert_unique_credentials
        attempts = []

        def pause_after_validation(conn, provider, values, exclude=None):
            original(conn, provider, values, exclude)
            if conn is self.conn:
                with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                    with second:
                        connections.create_connection(second, second_body)
                attempts.append(True)

        with patch.object(connections, '_assert_unique_credentials', side_effect=pause_after_validation):
            with self.conn:
                if deferred:
                    self.conn.execute('BEGIN')
                connections.create_connection(self.conn, first_body)
        self.assertEqual(attempts, [True])
        return second

    def test_duplicate_validation_serializes_independent_creates(self):
        second = self.interleave_create(self.body(), self.body())
        with self.assertRaisesRegex(ValueError, 'already'):
            with second:
                connections.create_connection(second, self.body())
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM provider_connections').fetchone()[0], 1)

    def test_deferred_create_admits_writer_before_duplicate_validation(self):
        second = self.interleave_create(self.body(), self.body(), deferred=True)
        with self.assertRaisesRegex(ValueError, 'already'):
            with second:
                connections.create_connection(second, self.body())

    def test_credential_rotation_cannot_race_another_rotation(self):
        first, other = self.create('first'), self.create('other')
        second = db.connect(self.path)
        self.addCleanup(second.close)
        second.execute('PRAGMA busy_timeout=0')
        replacement = connections.ConnectionPatch(credentials=self.body('replacement').credentials)
        original = connections._assert_unique_credentials
        attempts = []

        def pause_after_validation(conn, provider, values, exclude=None):
            original(conn, provider, values, exclude)
            if conn is self.conn:
                with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                    with second:
                        connections.update_connection(second, other['id'], replacement)
                attempts.append(True)

        with patch.object(connections, '_assert_unique_credentials', side_effect=pause_after_validation):
            with self.conn:
                self.conn.execute('BEGIN')
                connections.update_connection(self.conn, first['id'], replacement)
        self.assertEqual(attempts, [True])
        with self.assertRaisesRegex(ValueError, 'already'):
            with second:
                connections.update_connection(second, other['id'], replacement)
        self.assertEqual(connections.resolve_credentials(self.conn, first['id'])[1], replacement.credentials)
        self.assertEqual(connections.resolve_credentials(self.conn, other['id'])[1], self.body('other').credentials)

    def test_disconnect_admits_writer_before_lease_check(self):
        saved = self.create()
        second = db.connect(self.path)
        self.addCleanup(second.close)
        second.execute('PRAGMA busy_timeout=0')
        original = connections._assert_idle
        attempts = []

        def pause_after_validation(conn, identifier):
            original(conn, identifier)
            if conn is self.conn:
                with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                    with second:
                        connections.acquire_job(second, identifier)
                attempts.append(True)

        with patch.object(connections, '_assert_idle', side_effect=pause_after_validation):
            with self.conn:
                self.conn.execute('BEGIN')
                connections.disconnect_connection(self.conn, saved['id'])
        self.assertEqual(attempts, [True])
        self.assertEqual(connections.public_connection(self.conn, saved['id'])['status'], 'disconnected')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM provider_connection_jobs').fetchone()[0], 0)

    def test_active_lease_blocks_disconnect_and_rotation_without_changing_credentials(self):
        saved = self.create()
        second = db.connect(self.path)
        self.addCleanup(second.close)
        owner = connections.acquire_job(second, saved['id'])
        before = tuple(self.conn.execute('SELECT secrets,enabled,status FROM provider_connections WHERE id=?', (saved['id'],)).fetchone())
        for action in (lambda: connections.disconnect_connection(self.conn, saved['id']),
                       lambda: connections.update_connection(self.conn, saved['id'], connections.ConnectionPatch(credentials=self.body('changed').credentials))):
            with self.assertRaises(connections.ConnectionBusyError):
                with self.conn:
                    self.conn.execute('BEGIN')
                    action()
            self.assertEqual(tuple(self.conn.execute('SELECT secrets,enabled,status FROM provider_connections WHERE id=?', (saved['id'],)).fetchone()), before)
            self.assertEqual(self.conn.execute('SELECT owner FROM provider_connection_jobs WHERE connection_id=?', (saved['id'],)).fetchone()[0], owner)

    def test_late_refresh_cannot_change_disconnected_state(self):
        saved = self.create()
        self.save_cache(saved)
        with self.conn:
            connections.disconnect_connection(self.conn, saved['id'])
        with patch.object(connections, 'saved_connection_ids', return_value=[saved['id']]), \
                patch.object(connections.ProviderAdapter, 'sync') as adapter:
            self.assertEqual(connections.refresh_saved_connections('snaptrade', self.path), [])
        adapter.assert_not_called()
        metadata = connections.public_connection(self.conn, saved['id'])
        self.assertEqual(metadata['status'], 'disconnected')
        self.assertFalse(metadata['enabled'])
        self.assertIsNone(metadata['last_error'])
        self.assertTrue(snaptrade_store.load_portfolio(path=self.path)['accounts'][0]['connection_disconnected'])
        self.assertIsNone(self.conn.execute('SELECT owner FROM provider_connection_jobs WHERE connection_id=?', (saved['id'],)).fetchone()[0])

    def test_connection_mutations_preserve_caller_rollback(self):
        saved = self.create()
        before = dict(self.conn.execute('SELECT * FROM provider_connections WHERE id=?', (saved['id'],)).fetchone())
        for action in (lambda: connections.create_connection(self.conn, self.body('rolled-back')),
                       lambda: connections.update_connection(self.conn, saved['id'], connections.ConnectionPatch(credentials=self.body('changed').credentials)),
                       lambda: connections.disconnect_connection(self.conn, saved['id'])):
            self.conn.execute('BEGIN')
            self.conn.execute('SELECT id FROM provider_connections').fetchall()
            action()
            self.assertTrue(self.conn.in_transaction)
            self.conn.rollback()
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM provider_connections').fetchone()[0], 1)
            self.assertEqual(dict(self.conn.execute('SELECT * FROM provider_connections WHERE id=?', (saved['id'],)).fetchone()), before)

    def test_connection_limit_serializes_the_last_slot(self):
        for index in range(connections.MAX_SAVED_CONNECTIONS - 1):
            self.create(str(index))
        second = self.interleave_create(self.body('last'), self.body('overflow'))
        with self.assertRaisesRegex(ValueError, 'up to 50'):
            with second:
                connections.create_connection(second, self.body('overflow'))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM provider_connections').fetchone()[0], 50)

    def assert_refresh_failure(self, saved, expected):
        before = snaptrade_store.list_account_observations(path=self.path)
        with patch.object(connections.ProviderAdapter, 'sync', side_effect=AssertionError('No provider calls expected')) as adapter:
            results = connections.refresh_saved_connections('snaptrade', self.path)
        adapter.assert_not_called()
        self.assertEqual(len(results), 1)
        metadata = results[0]['connection']
        self.assertEqual(metadata['status'], 'error')
        self.assertIn(expected, metadata['last_error'])
        self.assertIsNotNone(metadata['last_checked_at'])
        cached = snaptrade_store.load_portfolio(path=self.path)
        self.assertTrue(cached['cache']['stale'])
        self.assertEqual(cached['cache']['refresh_error'], metadata['last_error'])
        self.assertEqual(cached['accounts'][0]['total_value'], 10)
        self.assertEqual(snaptrade_store.list_account_observations(path=self.path), before)
        self.assertIsNone(self.conn.execute('SELECT owner FROM provider_connection_jobs WHERE connection_id=?', (saved['id'],)).fetchone()[0])
        return metadata['last_error']

    def test_bad_encrypted_credentials_mark_cache_stale_and_reach_http_response(self):
        saved = self.create()
        self.save_cache(saved)
        with self.conn:
            self.conn.execute('UPDATE provider_connections SET secrets=? WHERE id=?', (b'broken-synthetic-ciphertext', saved['id']))
        self.assert_refresh_failure(saved, 'could not be decrypted')
        app = FastAPI()
        app.include_router(snaptrade_routes.router)
        with TestClient(app) as client:
            # The cooldown skips a second attempt but must preserve the failed
            # attempt's visible error and the last useful account data.
            response = client.post('/api/investments/connected/refresh')
        self.assertEqual(response.status_code, 200)
        self.assertIn('could not be decrypted', response.json()['cache']['refresh_error'])
        self.assertTrue(response.json()['cache']['stale'])
        self.assertEqual(len(response.json()['accounts']), 1)

    def test_wrong_restoration_key_is_reported_without_replacing_history(self):
        saved = self.create()
        self.save_cache(saved)
        self.key.write_bytes(Fernet.generate_key())
        self.assert_refresh_failure(saved, 'matching installation key')

    def test_decryptable_but_invalid_credentials_are_actionable(self):
        saved = self.create()
        self.save_cache(saved)
        with self.conn:
            self.conn.execute('UPDATE provider_connections SET secrets=? WHERE id=?',
                (Fernet(self.key.read_bytes()).encrypt(b'{"unexpected":"sensitive-synthetic-value"}'), saved['id']))
        error = self.assert_refresh_failure(saved, 'Saved credentials are invalid')
        self.assertNotIn('sensitive-synthetic-value', error)

    def test_hosted_credential_failure_is_sanitized_and_updates_cache(self):
        saved = self.create()
        self.save_cache(saved)
        with self.conn:
            self.conn.execute("UPDATE provider_connections SET auth_method='hosted_test' WHERE id=?", (saved['id'],))
        with patch('app.provider_oauth.resolve_hosted_credentials', side_effect=ValueError('SENSITIVE synthetic provider response')):
            error = self.assert_refresh_failure(saved, 'Provider authorization needs attention')
        self.assertNotIn('SENSITIVE', error)

    def test_missing_hosted_configuration_marks_saved_cache_stale(self):
        saved = self.create()
        self.save_cache(saved)
        with self.conn:
            self.conn.execute("UPDATE provider_connections SET auth_method='snaptrade_commercial' WHERE id=?", (saved['id'],))
        with patch.dict(os.environ, {'SNAPTRADE_COMMERCIAL_ID': '', 'SNAPTRADE_COMMERCIAL_KEY': ''}):
            self.assert_refresh_failure(saved, 'operator configuration')

    def test_active_lease_and_cooldown_skip_without_false_credential_errors(self):
        saved = self.create()
        self.save_cache(saved)
        owner = connections.acquire_job(self.conn, saved['id'])
        with patch.object(connections, 'resolve_credentials', side_effect=AssertionError('Lease must win')) as resolve:
            self.assertEqual(connections.refresh_saved_connections('snaptrade', self.path), [])
            with self.conn:
                self.conn.execute('UPDATE provider_connection_jobs SET owner=NULL,expires_at=NULL WHERE connection_id=?', (saved['id'],))
            self.assertEqual(connections.refresh_saved_connections('snaptrade', self.path), [])
        resolve.assert_not_called()
        self.assertEqual(connections.public_connection(self.conn, saved['id'])['status'], 'ready')
        self.assertFalse(snaptrade_store.load_portfolio(path=self.path)['cache']['stale'])
        self.assertIsNotNone(owner)

    def test_aggregate_selection_reaches_every_cached_reader_and_http(self):
        for suffix in ('first', 'second'):
            saved = self.create(suffix)
            self.save_cache(saved, count=16, suffix=suffix)
        cached = snaptrade_store.load_portfolio(path=self.path)
        identifiers = [row['id'] for row in cached['accounts']]
        self.assertEqual(len(identifiers), 32)
        for selected in (identifiers, identifiers[:1], [], ['__none__'], None):
            with self.subTest(selected=selected):
                kwargs = dict(path=self.path, cached=cached, account_ids=selected)
                results = [
                    investing_analytics.bundle(self.conn, today=date(2026, 9, 22), markets={}, **kwargs),
                    investing_calendar.bundle(self.conn, today=date(2026, 9, 22), **kwargs),
                    investment_coverage.bundle(self.conn, now=datetime(2026, 9, 22, tzinfo=timezone.utc), **kwargs),
                    market_valuation.bundle(self.conn, now=datetime(2026, 9, 22, tzinfo=timezone.utc), markets={}, **kwargs),
                ]
                expected = identifiers if selected is None else [] if selected == ['__none__'] else selected
                for result in results:
                    self.assertEqual(result['selected_account_ids'], expected)
        app = FastAPI()
        app.include_router(investing_routes.router)
        with TestClient(app) as client:
            response = client.get('/api/investments/analysis', params=[('account', value) for value in identifiers])
        self.assertEqual(response.status_code, 200, response.text[:300])
        self.assertEqual(response.json()['selected_account_ids'], identifiers)

    def test_aggregate_selection_is_bounded_and_rejects_unknown_or_malformed_ids(self):
        maximum = (connections.MAX_SAVED_CONNECTIONS + 1) * 30
        accounts = [{'id': 'account-' + str(index)} for index in range(maximum)]
        ids = [row['id'] for row in accounts]
        self.assertEqual(snaptrade_store.selected_account_ids(accounts, ids), ids)
        self.assertEqual(snaptrade_store.selected_account_ids(accounts, ids[:1] * 2), ids[:1])
        for invalid in (ids + ids[:1], ['missing'], [None], [['nested']], 'account-1', ['__none__', ids[0]]):
            with self.subTest(invalid=str(invalid)[:40]), self.assertRaisesRegex(ValueError, 'valid connected accounts'):
                snaptrade_store.selected_account_ids(accounts, invalid)


if __name__ == '__main__':
    unittest.main()

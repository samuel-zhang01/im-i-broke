"""Persistent cache and observation history using only synthetic portfolios."""
from contextlib import closing
import json
from datetime import date
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app import snaptrade_models as models
from app import snaptrade_routes as routes
from app import snaptrade_store as store


def portfolio(retrieved_at='2026-09-18T14:00:00+00:00', value=120):
    account = models.account({
        'id': 'synthetic-account', 'name': 'Synthetic ISA', 'institution_name': 'Example broker',
        'number': '12345678', 'balance': {'total': {'amount': value, 'currency': 'GBP'}},
        'sync_status': {'holdings': {'last_successful_sync': '2026-09-17T18:00:00+00:00'}},
    })
    account['positions'] = models.section([models.position({
        'instrument': {'id': 'instrument-one', 'kind': 'stock', 'symbol': 'TEST', 'description': 'Test security'},
        'units': 2, 'price': 50, 'cost_basis': 40, 'currency': 'GBP',
    })])
    account['balances'] = models.section([{'currency': 'GBP', 'cash': 20, 'buying_power': 200}])
    account['history'] = models.section([{
        'date': '2026-09-16', 'currency': 'GBP', 'total_value': 110, 'source': 'snaptrade_estimate',
    }])
    return {'configured': True, 'retrieved_at': retrieved_at, 'error': None, 'accounts': [account],
            'connections': models.section([{'id': 'connection', 'name': 'Example', 'institution': 'Example broker',
                                           'disabled': False, 'is_read_only': True, 'created_at': None}])}


class SnapTradeCacheTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'finance.db'
        for patcher in (patch.object(store.db, 'DB_PATH', self.path), patch.object(routes, '_next_read', 0.0)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_cache_survives_connections_and_only_allowlisted_display_data_is_saved(self):
        raw = portfolio()
        raw['consumer_key'] = 'synthetic-SECRET'
        raw['accounts'][0]['raw_data'] = {'secret': 'synthetic-SECRET'}
        raw['accounts'][0]['institution_account_id'] = 'synthetic-SECRET'
        raw['accounts'][0]['positions']['data'][0]['signed_url'] = 'synthetic-SECRET'
        saved = store.save_portfolio(raw)
        self.assertFalse(saved['cache']['is_cached'])
        loaded = store.load_portfolio()
        self.assertTrue(loaded['cache']['is_cached'])
        self.assertEqual(loaded['accounts'][0]['positions']['data'][0]['quantity'], 2)
        self.assertNotIn('synthetic-SECRET', json.dumps(loaded))
        self.assertEqual(loaded['accounts'][0]['number_suffix'], '5678')
        with closing(sqlite3.connect(self.path)) as connection, connection:
            dump = '\n'.join(connection.iterdump())
        self.assertNotIn('synthetic-SECRET', dump)
        self.assertNotIn('institution_account_id', dump)
        self.assertNotIn('consumer_key', dump)

    def test_history_is_idempotent_and_keeps_source_time_separate_from_retrieval(self):
        data = portfolio()
        store.save_portfolio(data)
        store.save_portfolio(data)
        snapshots = store.list_snapshots()
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]['position_count'], 1)
        observed = store.list_account_observations()
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]['observed_at'], '2026-09-17T18:00:00+00:00')
        self.assertEqual(observed[0]['retrieved_at'], '2026-09-18T14:00:00+00:00')
        self.assertEqual(observed[0]['source_time_known'], 1)
        snapshot = store.get_snapshot(snapshots[0]['id'])
        self.assertEqual(snapshot['accounts'][0]['positions']['data'][0]['value'], 100)
        self.assertEqual(snapshot['accounts'][0]['total_value'], 120)  # cash is not added again
        self.assertIsNone(store.get_snapshot(999999))

    def test_security_identity_survives_cache_and_snapshot_persistence(self):
        data = portfolio()
        position = models.position({
            'instrument': {'id': 'synthetic-option', 'kind': 'option', 'symbol': 'TEST-CALL',
                           'raw_symbol': 'TEST-CALL', 'currency': 'USD', 'exchange': 'XNAS',
                           'multiplier': 100,
                           'figi_instrument': {'figi_code': 'synthetic-venue-figi',
                                               'figi_share_class': 'synthetic-share-figi'}},
            'units': 2, 'price': 5, 'cost_basis': 4, 'currency': 'GBP',
        })
        data['accounts'][0]['positions'] = models.section([position])
        saved = store.save_portfolio(data)
        snapshot = store.get_snapshot(store.list_snapshots()[0]['id'])
        observed = store.list_position_observations()[0]
        for persisted in (saved['accounts'][0]['positions']['data'][0],
                          snapshot['accounts'][0]['positions']['data'][0], observed):
            for key in ('instrument_id', 'raw_symbol', 'listing_currency', 'exchange',
                        'figi_code', 'figi_share_class', 'multiplier'):
                self.assertEqual(persisted[key], position[key])
            self.assertEqual(persisted['listing_currency'], 'USD')
            self.assertEqual(persisted['currency'], 'GBP')
            self.assertEqual(persisted['value'], 1000)

    def test_old_persisted_account_numbers_are_masked_on_history_reads(self):
        store.save_portfolio(portfolio())
        full_number = '9876543210123456'
        label = 'Imported account ' + full_number
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('UPDATE snaptrade_account_observations SET name=?', (label,))
            connection.execute('UPDATE snaptrade_account_estimates SET name=?', (label,))
        snapshot = store.get_snapshot(store.list_snapshots()[0]['id'])
        observations = store.list_account_observations()
        estimates = store.list_account_estimates()
        for result in (snapshot, observations, estimates):
            self.assertNotIn(full_number, json.dumps(result))
        for account in (snapshot['accounts'][0], observations[0], estimates[0]):
            self.assertEqual(account['name'], 'Imported account …3456')

    def test_missing_source_time_is_explicit_and_paper_account_is_preserved(self):
        data = portfolio()
        data['accounts'][0]['holdings_synced_at'] = None
        data['accounts'][0]['total_value_as_of'] = None
        data['accounts'][0]['is_paper'] = True
        data['accounts'][0]['total_value'] = None
        store.save_portfolio(data)
        row = store.list_account_observations()[0]
        self.assertEqual(row['source_time_known'], 0)
        self.assertEqual(row['observed_at'], data['retrieved_at'])
        self.assertEqual(row['is_paper'], 1)
        self.assertIsNone(row['total_value'])

    def test_fresh_positions_do_not_redate_older_account_total(self):
        for day in (18, 19):
            data = portfolio(f'2026-09-{day}T14:00:00+00:00')
            account = data['accounts'][0]
            account['holdings_synced_at'] = f'2026-09-{day}T13:59:00+00:00'
            account['positions']['as_of'] = account['holdings_synced_at']
            store.save_portfolio(data)
        observations = store.list_account_observations()
        self.assertEqual(len(observations), 2)
        self.assertEqual({row['observed_at'] for row in observations}, {'2026-09-17T18:00:00+00:00'})
        self.assertEqual({row['total_value_as_of'] for row in observations}, {'2026-09-17T18:00:00+00:00'})
        self.assertEqual(store.list_account_observations(start='2026-09-18'), [])
        self.assertEqual([row['observed_at'] for row in store.list_position_observations()],
                         ['2026-09-18T13:59:00+00:00', '2026-09-19T13:59:00+00:00'])
        latest = store.get_snapshot(store.list_snapshots()[0]['id'])['accounts'][0]
        self.assertEqual(latest['observed_at'], '2026-09-17T18:00:00+00:00')
        self.assertEqual(latest['positions']['as_of'], '2026-09-19T13:59:00+00:00')
        self.assertTrue(latest['source_time_known'])

    def test_legacy_total_clock_stays_unknown_without_rewriting_saved_evidence(self):
        data = portfolio()
        del data['accounts'][0]['total_value_as_of']
        store.save_portfolio(data)
        # This is how older rows represented totals: a positions timestamp was
        # incorrectly applied to the account-list value. Do not claim it now.
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('UPDATE snaptrade_account_observations SET observed_at=?,source_time_known=1',
                               ('2026-09-17T18:00:00+00:00',))
        row = store.list_account_observations(start='2026-09-18')[0]
        self.assertIsNone(row['total_value_as_of'])
        self.assertEqual(row['observed_at'], data['retrieved_at'])
        self.assertEqual(row['source_time_known'], 0)
        self.assertEqual(store.list_account_observations(end='2026-09-17T23:59:59Z'), [])
        self.assertIsNone(store.load_portfolio()['accounts'][0]['total_value_as_of'])
        with closing(sqlite3.connect(self.path)) as connection, connection:
            saved = connection.execute('SELECT observed_at,source_time_known FROM snaptrade_account_observations').fetchone()
        self.assertEqual(saved, ('2026-09-17T18:00:00+00:00', 1))

    def test_failed_positions_keep_total_clock_and_empty_positions_keep_their_own_clock(self):
        store.save_portfolio(portfolio())
        update = portfolio('2026-09-19T14:00:00+00:00', value=125)
        account = update['accounts'][0]
        account['positions'] = models.section(error='Positions unavailable.')
        account['holdings_synced_at'] = '2026-09-19T13:59:00+00:00'
        store.save_portfolio(update)
        latest = store.get_snapshot(store.list_snapshots()[0]['id'])['accounts'][0]
        self.assertEqual(latest['total_value'], 125)
        self.assertEqual(latest['total_value_as_of'], '2026-09-17T18:00:00+00:00')
        self.assertEqual(latest['positions']['data'], [])
        self.assertIsNone(latest['positions']['as_of'])
        update = portfolio('2026-09-20T14:00:00+00:00')
        update['accounts'][0]['positions'] = {**models.section(), 'as_of': '2026-09-20T13:59:00+00:00'}
        store.save_portfolio(update)
        latest = store.get_snapshot(store.list_snapshots()[0]['id'])['accounts'][0]
        self.assertEqual(latest['positions']['data'], [])
        self.assertEqual(latest['positions']['as_of'], '2026-09-20T13:59:00+00:00')
        self.assertEqual(latest['observed_at'], '2026-09-17T18:00:00+00:00')

    def test_paper_mode_remains_boolean_in_archived_account_evidence(self):
        data = portfolio()
        data['accounts'][0]['is_paper'] = True
        store.save_portfolio(data)
        # A later account list can omit a paper account. Wealth then obtains
        # its mode from stored observations/estimates, not the active cache.
        removed = {**portfolio('2026-09-19T14:00:00+00:00'), 'accounts': []}
        store.save_portfolio(removed)
        self.assertIs(store.list_account_observations()[0]['is_paper'], True)
        self.assertIs(store.list_account_estimates()[0]['is_paper'], True)
        self.assertEqual(store.load_portfolio()['accounts'], [])

    def test_failed_section_retains_previous_rows_but_never_records_them_as_new_holdings(self):
        first = portfolio()
        first['accounts'][0]['positions']['as_of'] = '2026-09-18T13:59:00Z'
        store.save_portfolio(first)
        second = portfolio('2026-09-19T14:00:00+00:00', value=125)
        second['accounts'][0]['positions'] = models.section(error='The connection is temporarily unavailable.')
        result = store.save_portfolio(second)
        positions = result['accounts'][0]['positions']
        self.assertEqual(positions['status'], 'error')
        self.assertEqual(len(positions['data']), 1)
        self.assertTrue(positions['stale'])
        self.assertEqual(positions['cached_at'], first['retrieved_at'])
        self.assertEqual(positions['as_of'], '2026-09-18T13:59:00Z')
        self.assertEqual(result['accounts'][0]['holdings_synced_at'], positions['as_of'])
        self.assertTrue(result['cache']['stale'])
        self.assertEqual(len(store.list_position_observations()), 1)
        snapshot = store.get_snapshot(store.list_snapshots()[0]['id'])
        self.assertEqual(snapshot['accounts'][0]['positions']['status'], 'error')
        self.assertEqual(snapshot['accounts'][0]['positions']['data'], [])
        self.assertEqual(snapshot['accounts'][0]['total_value'], 125)

    def test_failed_read_preserves_previous_incomplete_positions_until_complete_success(self):
        first = portfolio()
        first['accounts'][0]['positions']['truncated'] = True
        store.save_portfolio(first)
        second = portfolio('2026-09-19T14:00:00Z')
        second['accounts'][0]['positions'] = models.section(error='Timed out.')
        retained = store.save_portfolio(second)['accounts'][0]['positions']
        self.assertTrue(retained['truncated'])
        self.assertTrue(retained['stale'])
        self.assertEqual(len(retained['data']), 1)
        complete = store.save_portfolio(portfolio('2026-09-19T14:10:00Z'))['accounts'][0]['positions']
        self.assertFalse(complete['truncated'])
        self.assertFalse(complete['stale'])

    def test_empty_success_replaces_previous_positions_and_preserves_truncation_in_history(self):
        store.save_portfolio(portfolio())
        data = portfolio('2026-09-19T14:00:00+00:00')
        data['accounts'][0]['positions'] = models.section([], truncated=True)
        result = store.save_portfolio(data)
        self.assertEqual(result['accounts'][0]['positions']['data'], [])
        self.assertFalse(result['accounts'][0]['positions']['stale'])
        newest = store.get_snapshot(store.list_snapshots()[0]['id'])
        self.assertTrue(newest['accounts'][0]['positions']['truncated'])
        self.assertEqual(newest['accounts'][0]['positions']['status'], 'empty')

    def test_interrupted_activity_paging_merges_ids_without_duplicate_rows(self):
        data = portfolio()
        data['accounts'][0]['activities'] = models.section([
            {'id': 'old', 'type': 'BUY', 'amount': -10}, {'id': 'shared', 'type': 'DIVIDEND', 'amount': 1},
        ])
        store.save_portfolio(data)
        update = portfolio('2026-09-19T14:00:00+00:00')
        update['accounts'][0]['activities'] = models.section([
            {'id': 'new', 'type': 'SELL', 'amount': 10}, {'id': 'shared', 'type': 'DIVIDEND', 'amount': 2},
        ], error='Later page failed.', truncated=True)
        result = store.save_portfolio(update)
        section = result['accounts'][0]['activities']
        self.assertEqual([row['id'] for row in section['data']], ['new', 'shared', 'old'])
        self.assertEqual(section['data'][1]['amount'], 2)
        self.assertTrue(section['stale'])
        self.assertTrue(section['truncated'])

    def test_estimates_keep_provenance_and_older_dates_outside_provider_window(self):
        store.save_portfolio(portfolio())
        second = portfolio('2026-09-19T14:00:00+00:00')
        second['accounts'][0]['history'] = models.section([
            {'date': '2026-09-17', 'currency': 'GBP', 'total_value': 115, 'source': 'untrusted-label'},
        ])
        store.save_portfolio(second)
        estimates = store.list_account_estimates(account_ids=['synthetic-account'])
        self.assertEqual([row['date'] for row in estimates], ['2026-09-16', '2026-09-17'])
        self.assertTrue(all(row['source'] == 'snaptrade_estimate' for row in estimates))
        self.assertEqual(len(store.list_account_estimates(start='2026-09-17', end='2026-09-18')), 1)
        self.assertEqual(store.list_account_estimates(account_ids=[]), [])
        self.assertEqual(store.list_account_observations(account_ids=['missing']), [])
        self.assertEqual(store.list_position_observations(account_id='missing'), [])

    def test_partial_history_keeps_valid_new_rows_without_reintroducing_rejected_dates(self):
        first = portfolio()
        store.save_portfolio(first)
        second = portfolio('2026-09-19T14:00:00+00:00')
        second['accounts'][0]['history'] = models.history({'currency': 'GBP', 'history': [
            {'date': '2026-09-16', 'total_value': 111},
            {'date': '2026-09-16', 'total_value': 112},
            {'date': '2026-09-17', 'total_value': 115},
            {'date': 'invalid', 'total_value': 9},
        ]}, today=date(2026, 9, 19))
        saved = store.save_portfolio(second)
        history = saved['accounts'][0]['history']
        self.assertEqual(history['status'], 'error')
        self.assertIn('omitted', history['error'])
        self.assertEqual([(row['date'], row['total_value']) for row in history['data']],
                         [('2026-09-17', 115)])
        self.assertFalse(history['stale'])
        self.assertEqual(history['cached_at'], second['retrieved_at'])
        estimates = store.list_account_estimates()
        self.assertEqual([(row['date'], row['total_value'], row['retrieved_at']) for row in estimates], [
            ('2026-09-16', 110, first['retrieved_at']),
            ('2026-09-17', 115, second['retrieved_at']),
        ])
        # A subsequent complete endpoint failure retains this subset without
        # presenting it as a new observation in the history archive.
        third = portfolio('2026-09-20T14:00:00+00:00')
        third['accounts'][0]['history'] = models.section(error='Unavailable')
        failed = store.save_portfolio(third)['accounts'][0]['history']
        self.assertTrue(failed['stale'])
        self.assertEqual(failed['cached_at'], second['retrieved_at'])
        self.assertEqual(failed['data'], history['data'])
        self.assertEqual(store.list_account_estimates(), estimates)

    def test_first_partial_history_response_archives_only_valid_rows(self):
        first = portfolio()
        first['accounts'][0]['history'] = models.history({'currency': 'GBP', 'history': [
            {'date': '2026-09-16', 'total_value': 0},
            {'date': '2026-09-17', 'total_value': 'NaN'},
        ]}, today=date(2026, 9, 18))
        saved = store.save_portfolio(first)
        self.assertEqual(saved['accounts'][0]['history']['status'], 'error')
        self.assertEqual([(row['date'], row['total_value']) for row in store.list_account_estimates()],
                         [('2026-09-16', 0)])

    def test_account_list_failure_keeps_cache_without_new_snapshot_and_marks_attempt(self):
        store.save_portfolio(portfolio())
        with patch.object(routes, 'configured', return_value=True), patch.object(routes, 'fetch_portfolio', return_value={
            'configured': True, 'retrieved_at': '2026-09-19T14:00:00+00:00', 'accounts': [],
            'connections': models.section(), 'error': 'The brokerage connection is unavailable.',
        }):
            response = routes.refresh_portfolio()
        self.assertEqual(response.status_code, 200)
        result = json.loads(response.body)
        self.assertEqual(len(result['accounts']), 1)
        self.assertEqual(result['retrieved_at'], '2026-09-18T14:00:00+00:00')
        self.assertTrue(result['cache']['stale'])
        self.assertTrue(result['cache']['is_cached'])
        self.assertEqual(result['cache']['refresh_error'], 'The brokerage connection is unavailable.')
        self.assertEqual(len(store.list_snapshots()), 1)
        with self.assertRaises(ValueError):
            store.save_portfolio({'configured': True, 'accounts': [], 'error': 'Failed'})

    def test_cached_get_is_not_blocked_by_refresh_lock_cooldown_or_missing_credentials(self):
        store.save_portfolio(portfolio())
        with patch.object(routes, 'configured', return_value=False), patch.object(routes, 'fetch_portfolio') as fetch:
            routes._lock.acquire()
            try:
                response = routes.connected_portfolio()
            finally:
                routes._lock.release()
            self.assertEqual(response.status_code, 200)
            result = json.loads(response.body)
            self.assertFalse(result['configured'])
            self.assertEqual(len(result['accounts']), 1)
            fetch.assert_not_called()

    def test_refresh_schedule_uses_runtime_configuration_without_rewriting_saved_data(self):
        store.save_portfolio(portfolio())
        before = store.load_portfolio()
        for enabled, interval in ((True, 600), (True, 1800), (True, 0), (False, 600)):
            with self.subTest(configured=enabled, interval=interval), \
                 patch.object(routes, 'configured', return_value=enabled), \
                 patch('app.portfolio_scheduler.sync_seconds', return_value=interval), \
                 patch.object(routes, 'fetch_portfolio') as fetch:
                response = routes.connected_portfolio()
                result = json.loads(response.body)
                self.assertEqual(result['refresh_schedule'], {
                    'enabled': bool(enabled and interval), 'interval_seconds': interval,
                })
                self.assertEqual(result['cache']['last_successful_refresh_at'],
                                 before['cache']['last_successful_refresh_at'])
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                fetch.assert_not_called()
        self.assertEqual(store.load_portfolio(), before)

    def test_refresh_schedule_does_not_promote_failed_attempt_to_success(self):
        store.save_portfolio(portfolio())
        with patch.object(routes, 'configured', return_value=True), \
             patch('app.portfolio_scheduler.sync_seconds', return_value=600), \
             patch.object(routes, 'fetch_portfolio', side_effect=RuntimeError('synthetic failure')):
            result = json.loads(routes.refresh_portfolio().body)
        self.assertEqual(result['refresh_schedule'], {'enabled': True, 'interval_seconds': 600})
        self.assertEqual(result['cache']['last_successful_refresh_at'], '2026-09-18T14:00:00+00:00')
        self.assertTrue(result['cache']['refresh_error'])
        self.assertEqual(len(store.list_snapshots()), 1)

    def test_unexpected_failure_retains_private_snapshot_without_exception_text(self):
        store.save_portfolio(portfolio())
        with patch.object(routes, 'configured', return_value=True), patch.object(routes, 'fetch_portfolio',
                                                                                side_effect=RuntimeError('synthetic-SECRET')):
            response = routes.refresh_portfolio()
        result = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(result['accounts']), 1)
        self.assertNotIn('synthetic-SECRET', json.dumps(result))
        self.assertTrue(result['cache']['stale'])

    def test_creating_portfolio_tables_preserves_existing_ledger_records(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('CREATE TABLE transactions(id INTEGER PRIMARY KEY, amount REAL)')
            connection.execute('INSERT INTO transactions VALUES(1,42)')
        store.save_portfolio(portfolio())
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute('SELECT id,amount FROM transactions').fetchall(), [(1, 42.0)])
            self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])


if __name__ == '__main__':
    unittest.main()

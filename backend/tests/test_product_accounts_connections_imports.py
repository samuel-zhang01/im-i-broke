"""Synthetic product setup boundaries: no production files or provider calls."""
import json
import os
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import account_settings as accounts, db, product_connections as connections, product_imports as imports
from app import lunchflow_client, lunchflow_routes, lunchflow_store, snaptrade_models, snaptrade_store
from app import snaptrade_client
from app import wealth, source_reconciliation


class ProductTests(unittest.TestCase):
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
        lunchflow_store.ensure_schema(self.conn)
        self.account = accounts.create_manual(self.conn, accounts.ManualAccount(display_name='Everyday', institution='Example Bank'))
        self.conn.commit()

    def bank_connection(self, secret='synthetic-secret'):
        with self.conn:
            return connections.create_connection(self.conn, connections.ConnectionCreate(
                provider='lunchflow', label='My banks', credentials={'api_key': secret}))

    def csv(self, text='date,description,amount\n2026-08-01,Coffee,-3.50\n', **kwargs):
        values = {'account_id': self.account['id'], 'csv_text': text,
                  'mapping': {'date_column': 'date', 'description_column': 'description', 'amount_column': 'amount'}, **kwargs}
        return imports.CsvPreview(**values)

    def commit(self, body, *, overlaps=False):
        preview = imports.prepare(self.conn, body)
        with self.conn:
            return imports.commit_import(self.conn, imports.CsvCommit(
                **body.model_dump(), preview_token=preview['preview_token'], confirm_overlaps=overlaps))

    def test_default_settings_are_compatible_and_names_are_editable(self):
        self.assertTrue(self.account['include_in_reports'])
        self.assertTrue(self.account['include_in_planning'])
        self.assertEqual(self.account['ownership_share'], 1)
        with self.conn:
            updated = accounts.save_settings(self.conn, self.account['id'], {'display_name': 'Joint cash', 'aliases': ['Monzo Bank'], 'ownership_share': .5})
        self.assertEqual(updated['display_name'], 'Joint cash')
        self.assertEqual(updated['aliases'], ['Monzo Bank'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM account_settings_history').fetchone()[0], 2)

    def test_historical_account_changes_are_previewed_and_rejected_without_rewriting(self):
        self.commit(self.csv())
        before = dict(self.conn.execute('SELECT * FROM transactions').fetchone())
        preview = accounts.preview_settings(self.conn, self.account['id'], {'currency': 'EUR', 'account_type': 'credit'})
        self.assertEqual(len(preview['blockers']), 2)
        with self.assertRaises(ValueError):
            with self.conn:
                accounts.save_settings(self.conn, self.account['id'], {'currency': 'EUR'})
        self.assertEqual(dict(self.conn.execute('SELECT * FROM transactions').fetchone()), before)
        self.assertEqual(accounts.account_view(self.conn, self.account['id'])['currency'], 'GBP')

    def test_dates_cannot_hide_existing_transactions(self):
        self.commit(self.csv())
        self.assertTrue(accounts.preview_settings(self.conn, self.account['id'], {'opened_on': '2026-09-01'})['blockers'])
        self.assertTrue(accounts.preview_settings(self.conn, self.account['id'], {'closed_on': '2026-07-31'})['blockers'])

    def test_dates_preserve_manual_and_booked_native_bank_evidence(self):
        with self.conn:
            self.conn.execute('INSERT INTO manual_balance_observations(account_id,amount,currency,as_of) VALUES(?,10,\'GBP\',\'2026-03-01\')', (self.account['id'],))
            raw = lunchflow_client.list_accounts({'accounts': [{'id': 'native', 'name': 'Cash', 'institution_name': 'Example Bank', 'currency': 'GBP'}]})
            lunchflow_store.discover_accounts(self.conn, raw, lunchflow_store.now())
            lunchflow_store.map_account(self.conn, 'native', account_id=self.account['id'])
            self.conn.execute('''INSERT INTO bank_transactions(connection_id,provider_id,txn_date,amount,currency,status,raw,first_seen_at,last_seen_at)
                VALUES('native','old-native','2026-02-01',20,'EUR','review','{}','2026-09-01','2026-09-01')''')
        self.assertTrue(accounts.preview_settings(self.conn, self.account['id'], {'opened_on': '2026-02-02'})['blockers'])
        self.assertTrue(accounts.preview_settings(self.conn, self.account['id'], {'closed_on': '2026-02-28'})['blockers'])
        self.assertFalse(accounts.preview_settings(self.conn, self.account['id'], {'opened_on': '2026-02-01', 'closed_on': '2026-03-01'})['blockers'])

    def test_explicit_account_currency_rejects_unknown_codes_without_writing(self):
        before = self.conn.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]
        with self.assertRaisesRegex(ValueError, 'supported'):
            accounts.create_manual(self.conn, accounts.ManualAccount(display_name='Invalid', currency='XYZ'))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM accounts').fetchone()[0], before)
        with self.assertRaisesRegex(ValueError, 'supported'):
            accounts.preview_settings(self.conn, self.account['id'], {'currency': 'XYZ'})

    def test_custom_institution_alias_allows_explicit_existing_bank_mapping(self):
        with self.conn:
            accounts.save_settings(self.conn, self.account['id'], {'aliases': ['Monzo Bank']})
            raw = lunchflow_client.list_accounts({'accounts': [{'id': 'one', 'name': 'Cash', 'institution_name': 'Monzo Bank', 'currency': 'GBP'}]})
            lunchflow_store.discover_accounts(self.conn, raw, lunchflow_store.now())
            lunchflow_store.map_account(self.conn, 'one', account_id=self.account['id'])
        self.assertEqual(self.conn.execute('SELECT account_id FROM bank_connections').fetchone()[0], self.account['id'])

    def test_saved_credentials_are_encrypted_and_public_projection_never_contains_them(self):
        connection = self.bank_connection()
        self.assertNotIn('synthetic-secret', json.dumps(connection))
        encrypted = self.conn.execute('SELECT secrets FROM provider_connections').fetchone()[0]
        self.assertNotIn(b'synthetic-secret', encrypted)
        self.assertEqual(connections.resolve_credentials(self.conn, connection['id'])[1]['api_key'], 'synthetic-secret')
        self.assertNotIn('synthetic-secret', '\n'.join(self.conn.iterdump()))

    def test_missing_key_does_not_generate_replacement_or_mutate_connection(self):
        connection = self.bank_connection()
        self.key.unlink()
        with self.assertRaisesRegex(ValueError, 'decrypted'):
            connections.resolve_credentials(self.conn, connection['id'])
        self.assertFalse(self.key.exists())
        self.assertEqual(connections.public_connection(self.conn, connection['id'])['status'], 'unverified')

    def test_unknown_and_extra_credential_fields_are_rejected(self):
        for provider, secret in (('unknown', {'api_key': 'x'}), ('lunchflow', {'api_key': 'x', 'url': 'https://example.com'}),
                                 ('lunchflow', {'api_key': 'contains\nnewline'})):
            with self.subTest(provider=provider, secret=secret), self.assertRaises(ValueError):
                with self.conn:
                    connections.create_connection(self.conn, connections.ConnectionCreate(provider=provider, label='Example', credentials=secret))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM provider_connections').fetchone()[0], 0)

    def test_validation_errors_do_not_echo_credentials(self):
        app = FastAPI()
        app.include_router(connections.router)
        with TestClient(app) as client:
            response = client.post('/api/connections', json={'provider': 'lunchflow', 'label': 'Example', 'credentials': {'api_key': ['TOP-SECRET']}})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('TOP-SECRET', response.text)

    def test_duplicate_credentials_are_rejected_without_duplicate_accounts(self):
        first = self.bank_connection()
        with self.assertRaisesRegex(ValueError, 'already'):
            self.bank_connection()
        second = self.bank_connection('second-synthetic-secret')
        with self.assertRaisesRegex(ValueError, 'already'):
            with self.conn:
                connections.update_connection(self.conn, second['id'], connections.ConnectionPatch(credentials={'api_key': 'synthetic-secret'}))
        self.assertEqual(connections.resolve_credentials(self.conn, first['id'])[1]['api_key'], 'synthetic-secret')
        with patch.dict(os.environ, {'LUNCH_FLOW_API': 'environment-secret'}):
            with self.assertRaisesRegex(ValueError, 'already'):
                self.bank_connection('environment-secret')

    def test_environment_connection_is_read_only_and_keeps_existing_values(self):
        with patch.dict(os.environ, {'LUNCH_FLOW_API': 'environment-secret'}):
            self.assertEqual(connections.public_connection(self.conn, 'environment:lunchflow')['source'], 'environment')
            with self.assertRaisesRegex(ValueError, 'server'):
                connections.disconnect_connection(self.conn, 'environment:lunchflow')
            with self.assertRaisesRegex(ValueError, 'environment'):
                connections.update_connection(self.conn, 'environment:lunchflow', connections.ConnectionPatch(label='Changed'))
            self.assertEqual(os.environ['LUNCH_FLOW_API'], 'environment-secret')

    def test_disconnect_removes_only_credentials_and_reconnect_keeps_identity(self):
        connection = self.bank_connection()
        with self.conn:
            result = connections.disconnect_connection(self.conn, connection['id'])
        self.assertEqual(result['status'], 'disconnected')
        self.assertIsNone(self.conn.execute('SELECT secrets FROM provider_connections').fetchone()[0])
        with self.assertRaisesRegex(ValueError, 'Reconnect'):
            connections.resolve_credentials(self.conn, connection['id'])
        with self.conn:
            reconnected = connections.update_connection(self.conn, connection['id'], connections.ConnectionPatch(credentials={'api_key': 'replacement'}))
        self.assertEqual(reconnected['id'], connection['id'])
        self.assertEqual(reconnected['status'], 'unverified')

    def test_provider_failure_is_sanitized_and_job_lease_is_released(self):
        connection = self.bank_connection()
        with patch.object(connections.ProviderAdapter, 'validate', side_effect=RuntimeError('TOP-SECRET signed response')):
            result = connections.perform_operation(connection['id'], 'validate', path=self.path)
        self.assertEqual(result['connection']['status'], 'error')
        self.assertNotIn('TOP-SECRET', json.dumps(result))
        self.assertIsNone(self.conn.execute('SELECT owner FROM provider_connection_jobs').fetchone()[0])
        with self.assertRaisesRegex(ValueError, 'minute'):
            connections.perform_operation(connection['id'], 'validate', path=self.path)

    def test_active_job_blocks_reconnect_and_disconnect(self):
        connection = self.bank_connection()
        connections.acquire_job(self.conn, connection['id'])
        with self.assertRaisesRegex(ValueError, 'running'):
            connections.disconnect_connection(self.conn, connection['id'])

    def test_csv_preview_is_read_only_then_import_is_idempotent_and_preserves_provenance(self):
        body = self.csv()
        preview = imports.prepare(self.conn, body)
        self.assertEqual(preview['new_count'], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 0)
        self.assertEqual(self.commit(body)['imported_count'], 1)
        self.assertEqual(self.commit(body)['imported_count'], 0)
        row = self.conn.execute('SELECT * FROM transactions').fetchone()
        self.assertEqual(row['amount'], -3.5)
        self.assertEqual(row['currency'], 'GBP')
        self.assertEqual(json.loads(row['meta'])['source'], 'csv')
        self.assertEqual(self.conn.execute('SELECT row_number FROM csv_import_rows').fetchone()[0], 2)

    def test_csv_retains_legitimate_identical_occurrences(self):
        body = self.csv('date,description,amount\n2026-08-01,Coffee,-3.50\n2026-08-01,Coffee,-3.50\n')
        self.assertEqual(self.commit(body)['imported_count'], 2)
        self.assertEqual(self.commit(body)['duplicate_count'], 2)

    def test_csv_rejects_ambiguous_dates_and_grouped_amounts(self):
        for text in ('date,description,amount\n01/08/26,Coffee,-3.5\n',
                     'date,description,amount\n2026-08-01,Coffee,"1,000.00"\n',
                     'date,description,amount\n2026-08-01,Coffee,NaN\n'):
            with self.subTest(text=text):
                preview = imports.prepare(self.conn, self.csv(text))
                self.assertFalse(preview['can_import'])
                self.assertEqual(preview['error_count'], 1)

    def test_csv_currency_mismatch_and_malformed_rows_do_not_partially_import(self):
        body = self.csv('date,description,amount\n2026-08-01,Coffee,-3.5\n2026-08-02,Incomplete\n')
        with self.assertRaisesRegex(ValueError, 'validation'):
            self.commit(body)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 0)
        body = self.csv(mapping={'date_column': 'date', 'description_column': 'description', 'amount_column': 'amount', 'currency': 'EUR'})
        self.assertFalse(imports.prepare(self.conn, body)['can_import'])

    def test_csv_derived_refresh_failure_rolls_back_batch_and_ledger_together(self):
        with patch('app.analytics.recompute_all', side_effect=RuntimeError('Synthetic failure')):
            with self.assertRaises(RuntimeError):
                self.commit(self.csv())
        for table in ('transactions', 'csv_import_batches', 'csv_import_rows'):
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)

    def test_saved_connection_pause_excludes_it_from_scheduling(self):
        connection = self.bank_connection()
        self.assertIn(connection['id'], connections.saved_connection_ids('lunchflow', self.path))
        with self.conn:
            connections.update_connection(self.conn, connection['id'], connections.ConnectionPatch(enabled=False))
        self.assertNotIn(connection['id'], connections.saved_connection_ids('lunchflow', self.path))
        with self.assertRaisesRegex(ValueError, 'enable'):
            connections.resolve_credentials(self.conn, connection['id'])

    def test_native_currency_is_preserved_in_separate_account(self):
        with self.conn:
            native = accounts.create_manual(self.conn, accounts.ManualAccount(display_name='Euro cash', currency='EUR'))
        body = self.csv(account_id=native['id'], mapping={'date_column': 'date', 'description_column': 'description', 'amount_column': 'amount', 'currency': 'EUR'})
        self.commit(body)
        self.assertEqual(self.conn.execute('SELECT currency,amount FROM transactions').fetchone()[:], ('EUR', -3.5))

    def test_source_id_revision_preserves_previous_ledger_row(self):
        kwargs = {'mapping': {'date_column': 'date', 'description_column': 'description', 'amount_column': 'amount', 'id_column': 'id'}}
        self.commit(self.csv('id,date,description,amount\nx,2026-08-01,Coffee,-3.5\n', **kwargs))
        revised = self.csv('id,date,description,amount\nx,2026-08-01,Coffee,-7\n', **kwargs)
        preview = imports.prepare(self.conn, revised)
        self.assertFalse(preview['can_import'])
        self.assertIn('changed', preview['errors'][0]['message'])
        self.assertEqual(self.conn.execute('SELECT amount FROM transactions').fetchone()[0], -3.5)

    def test_csv_requires_fresh_preview_and_explicit_overlap_choice(self):
        body = self.csv()
        original = imports.prepare(self.conn, body)
        with self.conn:
            self.conn.execute('''INSERT INTO transactions(account_id,txn_date,description,amount,dedupe_key)
                VALUES(?,'2026-08-01','Other evidence',-3.5,'other')''', (self.account['id'],))
        with self.assertRaisesRegex(ValueError, 'changed'):
            with self.conn:
                imports.commit_import(self.conn, imports.CsvCommit(**body.model_dump(), preview_token=original['preview_token']))
        self.assertEqual(imports.prepare(self.conn, body)['review_count'], 1)
        with self.assertRaisesRegex(ValueError, 'overlaps'):
            self.commit(body)
        self.assertEqual(self.commit(body, overlaps=True)['imported_count'], 1)

    def test_explicit_debit_credit_and_decimal_separator(self):
        body = self.csv('date;description;debit;credit\n01/08/2026;Coffee;3,50;\n02/08/2026;Salary;;100,00\n',
                        mapping={'date_column': 'date', 'description_column': 'description', 'debit_column': 'debit',
                                 'credit_column': 'credit', 'delimiter': ';', 'decimal_separator': ',', 'date_format': '%d/%m/%Y'})
        self.commit(body)
        self.assertEqual([r[0] for r in self.conn.execute('SELECT amount FROM transactions ORDER BY id')], [-3.5, 100])

    def test_bank_discovery_and_sync_are_scoped_to_credential_set(self):
        def read(operation, account_id=None):
            if operation == 'accounts':
                return {'accounts': [{'id': 'shared', 'name': 'Main', 'institution_name': 'Example Bank', 'currency': 'GBP'}]}
            if operation == 'balance':
                return {'balance': {'amount': 12, 'currency': 'GBP'}}
            return {'transactions': [{'id': 'tx', 'accountId': account_id, 'date': '2026-08-01', 'amount': -2, 'currency': 'GBP', 'description': 'Coffee'}]}
        for scope in ('environment:lunchflow', 'connection:one', 'connection:two'):
            lunchflow_routes.run_sync(read, self.path, provider_connection_id=scope)
        found = self.conn.execute('SELECT id,present,provider_connection_id,external_id FROM bank_connections ORDER BY id').fetchall()
        self.assertEqual(len(found), 3)
        self.assertTrue(all(row['present'] for row in found))
        self.assertEqual({row['external_id'] for row in found}, {'shared'})
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM bank_transactions').fetchone()[0], 3)
        with self.conn:
            lunchflow_store.discover_accounts(self.conn, [], lunchflow_store.now(), 'connection:one')
        self.assertEqual(self.conn.execute("SELECT present FROM bank_connections WHERE id='shared'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT present FROM bank_connections WHERE id='connection:two:shared'").fetchone()[0], 1)

    def test_scoped_snaptrade_cache_preserves_other_snapshots_and_source_clocks(self):
        def portfolio(account_id, stamp, value):
            account = snaptrade_models.account({'id': account_id, 'name': 'ISA', 'institution_name': 'Broker',
                'balance': {'total': {'amount': value, 'currency': 'GBP'}}})
            account['positions'] = snaptrade_models.section([])
            return {'configured': True, 'retrieved_at': stamp, 'accounts': [account], 'connections': snaptrade_models.section([])}
        with self.conn:
            saved = connections.create_connection(self.conn, connections.ConnectionCreate(provider='snaptrade', label='Broker',
                credentials={'client_id': 'client', 'consumer_key': 'consumer'}))
        legacy_stamp, saved_stamp = '2026-08-01T12:00:00+00:00', '2026-08-02T12:00:00+00:00'
        snaptrade_store.save_portfolio(portfolio('legacy', legacy_stamp, 10), path=self.path)
        snaptrade_store.save_portfolio(portfolio(saved['id'] + ':one', saved_stamp, 20), path=self.path, connection_scope=saved['id'])
        payload = snaptrade_store.load_portfolio(path=self.path)
        self.assertEqual(len(payload['accounts']), 2)
        self.assertEqual({a['id']: a['retrieved_at'] for a in payload['accounts']}, {'legacy': legacy_stamp, saved['id'] + ':one': saved_stamp})
        records = snaptrade_store.list_account_observations(path=self.path)
        self.assertEqual(len(records), 2)
        self.assertEqual({r['retrieved_at'] for r in records}, {legacy_stamp, saved_stamp})
        snaptrade_store.save_portfolio(portfolio('legacy', '2026-08-03T12:00:00+00:00', 11), path=self.path)
        self.assertEqual(len(snaptrade_store.load_portfolio(path=self.path)['accounts']), 2)
        scoped = snaptrade_store.load_portfolio(path=self.path, connection_scope=saved['id'])
        self.assertEqual(scoped['accounts'][0]['total_value'], 20)
        self.assertEqual(scoped['retrieved_at'], saved_stamp)
        self.assertEqual(len(snaptrade_store.list_account_observations(path=self.path)), 3)

    def test_manual_balance_and_settings_routes_preserve_prior_observations(self):
        app = FastAPI()
        app.include_router(accounts.router)
        with TestClient(app) as client:
            created = client.post('/api/workspace/accounts', json={'display_name': 'Cash wallet', 'currency': 'GBP'}).json()
            for amount in (20, 23):
                response = client.post(f"/api/workspace/accounts/{created['id']}/balances", json={'amount': amount, 'currency': 'GBP', 'as_of': '2026-08-01'})
                self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json()['manual_balance']['amount'], 23)
            self.assertEqual(client.patch(f"/api/workspace/accounts/{created['id']}", json={'currency': 'USD'}).status_code, 409)
            self.assertEqual(client.patch(f"/api/workspace/accounts/{created['id']}", json={'display_name': 'Pocket money'}).status_code, 200)
            self.assertEqual(client.get('/api/workspace/accounts').status_code, 200)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM manual_balance_observations').fetchone()[0], 2)
        report = wealth.bundle(self.conn, path=self.path, today=date(2026, 8, 1), account_ids=['statement:' + created['id']])
        self.assertEqual(report['summary']['net_worth'], 23)

    def test_disconnected_broker_history_is_retained_without_double_counting_reconnect(self):
        def portfolio(scope, names, stamp):
            rows = []
            for identifier, value in names:
                row = snaptrade_models.account({'id': (scope + ':' if scope else '') + identifier,
                    'name': 'Broker account', 'institution_name': 'Example broker',
                    'balance': {'total': {'amount': value, 'currency': 'GBP'}}})
                row['positions'] = snaptrade_models.section([])
                rows.append(row)
            return {'configured': True, 'retrieved_at': stamp, 'accounts': rows, 'connections': snaptrade_models.section([])}
        with self.conn:
            old = connections.create_connection(self.conn, connections.ConnectionCreate(provider='snaptrade', label='Old broker',
                credentials={'client_id': 'old-client', 'consumer_key': 'old-consumer'}))
            new = connections.create_connection(self.conn, connections.ConnectionCreate(provider='snaptrade', label='New broker',
                credentials={'client_id': 'new-client', 'consumer_key': 'new-consumer'}))
        snaptrade_store.save_portfolio(portfolio(old['id'], [('same-id', 20), ('changed-old-id', 30)], '2026-09-18T12:00:00+00:00'),
                                      path=self.path, connection_scope=old['id'])
        response = portfolio(None, [('same-id', 25), ('changed-new-id', 35)], '2026-09-19T12:00:00+00:00')
        with patch.object(snaptrade_client, 'fetch_portfolio', return_value=response):
            with self.assertRaisesRegex(ValueError, 'already'):
                connections.PROVIDERS['snaptrade'].sync({}, new['id'], self.path)
        with self.conn:
            connections.update_connection(self.conn, old['id'], connections.ConnectionPatch(enabled=False))
        sources, _ = wealth.sources(self.conn, path=self.path, today=date(2026, 9, 19))
        old_sources = [row for row in sources if row['id'].startswith('connected:' + old['id'])]
        self.assertTrue(all(row['default_included'] and row['forced_stale'] for row in old_sources))
        with self.assertRaisesRegex(ValueError, 'already'):
            snaptrade_store.save_portfolio(portfolio(new['id'], [('same-id', 25)], '2026-09-19T12:00:00+00:00'),
                                          path=self.path, connection_scope=new['id'])
        original = snaptrade_store.list_account_observations(path=self.path)
        with self.conn:
            connections.disconnect_connection(self.conn, old['id'])
        with patch.object(snaptrade_client, 'fetch_portfolio', return_value=response):
            connections.PROVIDERS['snaptrade'].sync({}, new['id'], self.path)
        report = wealth.bundle(self.conn, path=self.path, today=date(2026, 9, 19))
        self.assertEqual(report['summary']['net_worth'], 60)
        self.assertFalse(any(identifier.startswith('connected:' + old['id']) for identifier in report['selected_account_ids']))
        retained = [row for row in report['accounts'] if row['id'].startswith('connected:' + old['id'])]
        self.assertEqual(len(retained), 2)
        self.assertTrue(all(not row['default_included'] and 'Disconnected' in row['exclusion_reason'] for row in retained))
        observations = snaptrade_store.list_account_observations(path=self.path)
        self.assertEqual(len(observations), 4)
        self.assertTrue(all(row in observations for row in original))

    def test_csv_route_preview_commit_and_reusable_template(self):
        app = FastAPI()
        app.include_router(imports.router)
        body = self.csv().model_dump()
        body['source_filename'] = 'monthly-export.csv'
        with TestClient(app) as client:
            preview = client.post('/api/workspace/imports/preview', json=body)
            self.assertEqual(preview.status_code, 200)
            self.assertNotIn('_raw', preview.text)
            response = client.post('/api/workspace/imports/commit', json={**body, 'preview_token': preview.json()['preview_token']})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['imported_count'], 1)
            response = client.post('/api/workspace/imports/templates', json={'name': 'My bank format', 'mapping': body['mapping']})
            self.assertEqual(response.status_code, 201)
            self.assertEqual(client.get('/api/workspace/imports/templates').json()['templates'][0]['name'], 'My bank format')
        self.assertEqual(self.conn.execute('SELECT source_filename FROM csv_import_batches').fetchone()[0], 'monthly-export.csv')

    def test_existing_remote_bank_account_is_held_when_seen_under_new_credentials(self):
        with self.conn:
            raw = lunchflow_client.list_accounts({'accounts': [{'id': 'one', 'name': 'Main', 'institution_name': 'Example Bank', 'currency': 'GBP'}]})
            lunchflow_store.discover_accounts(self.conn, raw, lunchflow_store.now())
            lunchflow_store.map_account(self.conn, 'one', account_id=self.account['id'])
            scoped = [{**raw[0], 'id': 'connection:other:one', 'external_id': 'one'}]
            lunchflow_store.discover_accounts(self.conn, scoped, lunchflow_store.now(), 'connection:other')
            lunchflow_store.record_account_result(self.conn, scoped[0]['id'], {'balance': {'amount': 100, 'currency': 'GBP'}, 'transactions': [], 'errors': []}, lunchflow_store.now())
        duplicate = dict(self.conn.execute("SELECT * FROM bank_connections WHERE id='connection:other:one'").fetchone())
        self.assertIsNone(duplicate['account_id'])
        self.assertIn('another connection', lunchflow_store.mapping_reason(self.conn, duplicate))

    def test_commercial_snaptrade_injects_saved_user_credentials_and_retains_readonly_transport(self):
        client = Mock()
        client.account_information.list_user_accounts.return_value.body = []
        credentials = {'auth_method': 'snaptrade_commercial', 'client_id': 'client', 'consumer_key': 'consumer',
                       'user_id': 'saved-user', 'user_secret': 'saved-secret'}
        with patch.object(snaptrade_client, 'create_client', return_value=client):
            self.assertEqual(snaptrade_client.call('accounts', credentials=credentials, user_id='wrong-user'), [])
        client.account_information.list_user_accounts.assert_called_once_with(user_id='saved-user', user_secret='saved-secret')
        client.account_information.api_client.rest_client.pool_manager.clear.assert_called_once()

    def test_key_resolution_is_serialized_under_job_lease(self):
        connection = self.bank_connection()
        original = connections.resolve_credentials
        def resolve(conn, connection_id):
            self.assertIsNotNone(conn.execute('SELECT owner FROM provider_connection_jobs WHERE connection_id=?', (connection_id,)).fetchone()[0])
            return original(conn, connection_id)
        with patch.object(connections, 'resolve_credentials', side_effect=resolve), patch.object(connections.ProviderAdapter, 'validate', return_value=0):
            result = connections.perform_operation(connection['id'], 'validate', path=self.path)
        self.assertEqual(result['connection']['status'], 'ready')

    def linked_investment(self, share=.5):
        with self.conn:
            account = accounts.create_manual(self.conn, accounts.ManualAccount(display_name='Joint investment', institution='Example broker', account_type='investment'))
            accounts.save_settings(self.conn, account['id'], {'ownership_share': share})
            self.conn.execute('''INSERT INTO statements(account_id,source_file,period_end,reconciled,meta)
                VALUES(?,'joint-statement.pdf','2026-09-16',1,?)''', (account['id'], json.dumps({
                    'parser': 'moomoo', 'base_currency': 'GBP', 'net_asset_value': 80, 'native_reconciled': True})))
        broker = snaptrade_models.account({'id': 'broker', 'name': 'Joint broker', 'institution_name': 'Example broker',
            'balance': {'total': {'amount': 100, 'currency': 'GBP'}}})
        snaptrade_store.save_portfolio({'configured': True, 'retrieved_at': '2026-09-18T12:00:00+00:00',
            'accounts': [broker], 'connections': snaptrade_models.section([])}, path=self.path)
        source_accounts, _ = wealth.sources(self.conn, path=self.path, today=date(2026, 9, 18))
        source_reconciliation.set_link(self.conn, source_id='statement:' + account['id'], canonical_id='connected:broker',
            evidence_kind='owner_confirmed', evidence_digest='a' * 64, accounts=source_accounts)
        return account

    def test_linked_account_ownership_applies_once_to_canonical_balance_and_history(self):
        account = self.linked_investment()
        report = wealth.bundle(self.conn, path=self.path, today=date(2026, 9, 18), account_ids=['statement:' + account['id'], 'connected:broker'])
        self.assertEqual(report['selected_account_ids'], ['connected:broker'])
        self.assertEqual(report['summary']['net_worth'], 50)
        by_id = {row['id']: row for row in report['accounts']}
        self.assertEqual(by_id['connected:broker']['ownership_share'], .5)
        self.assertEqual(by_id['connected:broker']['full_native_value'], 100)
        dated = {row['date']: row for row in report['points']}
        self.assertEqual(dated['2026-09-16']['net_worth'], 40)
        with self.conn:
            accounts.save_settings(self.conn, account['id'], {'include_in_reports': False})
        sources, _ = wealth.sources(self.conn, path=self.path, today=date(2026, 9, 18))
        canonical = next(row for row in sources if row['id'] == 'connected:broker')
        self.assertFalse(canonical['default_included'])

    def test_conflicting_linked_ownership_remains_unknown_with_explicit_warning(self):
        self.linked_investment()
        with self.conn:
            other = accounts.create_manual(self.conn, accounts.ManualAccount(display_name='Other source', account_type='investment'))
            accounts.save_settings(self.conn, other['id'], {'ownership_share': .25})
        sources, _ = wealth.sources(self.conn, path=self.path, today=date(2026, 9, 18))
        source_reconciliation.set_link(self.conn, source_id='statement:' + other['id'], canonical_id='connected:broker',
            evidence_kind='owner_confirmed', evidence_digest='b' * 64, accounts=sources)
        report = wealth.bundle(self.conn, path=self.path, today=date(2026, 9, 18), account_ids=['connected:broker'])
        self.assertIsNone(report['summary']['net_worth'])
        self.assertTrue(report['summary']['partial'])
        self.assertTrue(any('conflicting ownership shares' in warning for warning in report['warnings']))

    def test_bank_scheduler_tracks_environment_attempt_separately(self):
        owner, run = lunchflow_store.acquire_sync(self.conn)
        lunchflow_store.finish_sync(self.conn, owner, run)
        with self.conn:
            self.conn.execute("UPDATE bank_sync_runs SET started_at='2026-01-01T00:00:00+00:00'")
        owner, run = lunchflow_store.acquire_sync(self.conn, provider_connection_id='connection:other')
        lunchflow_store.finish_sync(self.conn, owner, run)
        self.assertEqual(lunchflow_store.sync_state(self.conn, 'environment:lunchflow')['last_attempt_at'], '2026-01-01T00:00:00+00:00')
        self.assertNotEqual(lunchflow_store.sync_state(self.conn)['last_attempt_at'], '2026-01-01T00:00:00+00:00')


if __name__ == '__main__':
    unittest.main()

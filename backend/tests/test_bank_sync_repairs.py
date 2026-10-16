"""Bank recovery, historical links and institution types, using isolated SQLite."""
from contextlib import closing
from datetime import datetime, timedelta, timezone, date
import os
from pathlib import Path
import tempfile
from threading import Event, Thread, current_thread
import unittest
from unittest.mock import patch

from app import db, lunchflow_client as client, lunchflow_store as store, lunchflow_routes as routes
from app import bank_balance_rules, banking_health, planning, product_connections


class BankSyncRepairTests(unittest.TestCase):
    def setUp(self):
        self.folder = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.folder) / 'synthetic.db'
        self.conn = db.init_db(self.path)
        self.addCleanup(self.conn.close)
        self.enterContext(patch.dict(os.environ, {'LUNCH_FLOW_API': '', 'LUNCH_FLOW_SYNC_SECONDS': '21600'}))

    def discover(self, keys, bank='American Express', name='Gold Card', currency='GBP'):
        with self.conn:
            store.discover_accounts(self.conn, client.list_accounts({'accounts': [
                {'id': key, 'name': name, 'institution_name': bank, 'currency': currency, 'status': 'ACTIVE'}
                for key in keys]}), '2026-10-01T12:00:00+00:00')

    def save(self, key, amount=100, currency='GBP'):
        raw = {'id': 'purchase', 'accountId': key, 'date': '2026-10-01', 'amount': -10,
               'currency': currency, 'description': 'Synthetic shop', 'isPending': False}
        with self.conn:
            store.record_account_result(self.conn, key, {'balance': {'amount': amount, 'currency': currency},
                'transactions': client.transactions({'transactions': [raw]}, key), 'errors': []},
                '2026-10-01T12:01:00+00:00')

    def original_card(self):
        self.discover(['old'])
        self.save('old')
        with self.conn:
            store.map_account(self.conn, 'old', create_new=True, account_type='credit', balance_multiplier=-1)
        return self.conn.execute("SELECT account_id FROM bank_connections WHERE id='old'").fetchone()[0]

    def test_successor_preserves_account_rows_reviews_raw_balances_and_old_links(self):
        local = self.original_card()
        with self.conn:
            self.conn.execute("UPDATE transactions SET category='My choice',category_source='user'")
        ledger = [tuple(row) for row in self.conn.execute('SELECT * FROM transactions')]
        original_raw = self.conn.execute("SELECT raw FROM bank_transactions WHERE connection_id='old'").fetchone()[0]
        self.discover(['new'])
        self.save('new')
        with self.conn:
            store.map_account(self.conn, 'new', account_id=local, balance_multiplier=-1,
                              replace_connection_id='old', confirm_reconnection=True)
        self.assertEqual([tuple(row) for row in self.conn.execute('SELECT * FROM transactions')], ledger)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM accounts').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM bank_balance_observations').fetchone()[0], 2)
        links = [tuple(row) for row in self.conn.execute('SELECT connection_id,transaction_id,historical FROM bank_transactions ORDER BY id')]
        self.assertEqual(links, [('old', 1, 1), ('new', 1, 0)])
        self.assertEqual(self.conn.execute("SELECT raw FROM bank_transactions WHERE connection_id='old'").fetchone()[0], original_raw)
        self.assertEqual(len(store.latest_balances(self.conn)), 1)
        self.assertEqual(planning.account_snapshot(self.conn, date(2026, 10, 5))[0]['balance'], -100)
        self.assertEqual(sum(banking_health.snapshot(self.conn)['integrity_issues'].values()), 0)
        self.assertEqual(self.conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(self.conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_reconnection_requires_confirmation_and_duplicate_creation_is_guarded(self):
        local = self.original_card()
        self.discover(['new'])
        self.save('new')
        for kwargs in ({'account_id': local, 'replace_connection_id': 'old'},
                       {'create_new': True, 'account_type': 'credit'}):
            with self.assertRaises(ValueError), self.conn:
                store.map_account(self.conn, 'new', balance_multiplier=-1, **kwargs)
        self.assertEqual(self.conn.execute("SELECT excluded FROM bank_connections WHERE id='old'").fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM accounts').fetchone()[0], 1)

    def test_active_predecessor_and_running_sync_cannot_be_replaced(self):
        local = self.original_card()
        self.discover(['old', 'new'])
        self.save('new')
        with self.assertRaisesRegex(ValueError, 'still active'), self.conn:
            store.map_account(self.conn, 'new', account_id=local, balance_multiplier=-1,
                              replace_connection_id='old', confirm_reconnection=True)
        self.discover(['new'])
        owner, run = store.acquire_sync(self.conn)
        with self.assertRaisesRegex(ValueError, 'syncing'), self.conn:
            store.map_account(self.conn, 'new', account_id=local, balance_multiplier=-1,
                              replace_connection_id='old', confirm_reconnection=True)
        store.finish_sync(self.conn, owner, run)

    def test_successor_inherits_card_rule_without_rewriting_historical_valuations(self):
        local = self.original_card()
        with self.conn:
            bank_balance_rules.set_rule(self.conn, 'old', balance_type='available_credit', credit_limit=1000, currency='GBP')
        stamp = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        with self.conn:
            store.record_account_result(self.conn, 'old', {'balance': {'amount': 900, 'currency': 'GBP'},
                'transactions': [], 'errors': []}, stamp)
        valuations = [tuple(row) for row in self.conn.execute('SELECT * FROM bank_credit_valuations')]
        self.discover(['new'])
        self.save('new', amount=900)
        with self.conn:
            store.map_account(self.conn, 'new', account_id=local, balance_multiplier=-1,
                              replace_connection_id='old', confirm_reconnection=True)
        self.assertEqual(bank_balance_rules.get_rule(self.conn, 'new')['credit_limit'], 1000)
        self.assertEqual(store.latest_balances(self.conn)[local]['amount'], -100)
        self.assertEqual([tuple(row) for row in self.conn.execute('SELECT * FROM bank_credit_valuations')], valuations)

    def test_explicit_review_on_a_historical_feed_is_rejected(self):
        local = self.original_card()
        self.discover(['new']); self.save('new')
        with self.conn:
            store.map_account(self.conn, 'new', account_id=local, balance_multiplier=-1,
                              replace_connection_id='old', confirm_reconnection=True)
            self.conn.execute("UPDATE bank_transactions SET status='review' WHERE connection_id='old'")
        old_id = self.conn.execute("SELECT id FROM bank_transactions WHERE connection_id='old'").fetchone()[0]
        with self.assertRaisesRegex(ValueError, 'historical'), self.conn:
            store.resolve_review(self.conn, old_id, 'import')

    def test_amex_savings_are_cash_and_credit_names_still_require_confirmation(self):
        self.discover(['savings'], name='Personal Savings', currency='USD')
        self.save('savings', currency='USD')
        self.assertEqual(self.conn.execute('SELECT account_type,currency FROM accounts').fetchone()[:], ('savings', 'USD'))
        self.assertFalse(store.needs_balance_direction({'institution_name': 'American Express', 'name': 'Personal Savings'}))
        self.assertTrue(store.needs_balance_direction({'institution_name': 'American Express', 'name': 'Savings Credit Card'}))
        self.assertTrue(store.needs_balance_direction({'institution_name': 'American Express', 'name': 'Gold Card'}))

    def test_saved_health_includes_leases_and_is_read_only_without_secrets(self):
        stamp = datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute("INSERT INTO provider_connections(id,provider,label,secrets,enabled,status,created_at,updated_at) VALUES('connection:health','lunchflow','Private label',?,1,'ready',?,?)", (b'private-unused-credential', stamp, stamp))
        store.acquire_sync(self.conn, provider_connection_id='connection:health')
        before = list(self.conn.iterdump())
        self.conn.execute('PRAGMA query_only=ON')
        result = banking_health.snapshot(self.conn)
        self.assertTrue(result['configured']); self.assertTrue(result['scheduler']['enabled']); self.assertTrue(result['scheduler']['running'])
        self.assertNotIn('Private label', str(result)); self.assertNotIn('private-unused-credential', str(result))
        self.assertEqual(before, list(self.conn.iterdump()))

    def test_saved_transient_failures_retry_but_credentials_and_disabled_polling_do_not(self):
        recent = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        with self.conn:
            self.conn.execute("INSERT INTO provider_connections(id,provider,label,secrets,enabled,status,created_at,updated_at,last_checked_at,last_error) VALUES('connection:retry','lunchflow','Synthetic feed',?,1,'error',?,?,?,?)", (b'unused', recent, recent, recent, 'Lunch Flow is temporarily unavailable.'))
        with patch.object(db, 'DB_PATH', self.path):
            self.assertEqual(product_connections._scheduled_connections(), ['connection:retry'])
            with self.conn:
                self.conn.execute("UPDATE provider_connections SET last_error='Lunch Flow rejected the API key.'")
            self.assertEqual(product_connections._scheduled_connections(), [])
            with patch.dict(os.environ, {'LUNCH_FLOW_SYNC_SECONDS': '0'}):
                self.assertEqual(product_connections._scheduled_connections(), [])

    def test_two_connections_recover_failed_derived_work_on_an_idempotent_retry(self):
        with self.conn:
            self.conn.execute("INSERT INTO rules(pattern,category,priority) VALUES('Synthetic shop','Groceries',1)")
        waiting, release = Event(), Event()
        failures, results = [], {}
        original = routes.refresh_derived
        def reader(key):
            def read(operation, account_id=None):
                if operation == 'accounts':
                    return {'accounts': [{'id': key, 'name': 'Current', 'institution_name': 'Synthetic '+key, 'currency': 'GBP', 'status': 'ACTIVE'}]}
                if operation == 'balance':
                    return {'balance': {'amount': 100, 'currency': 'GBP'}}
                if key == 'B':
                    waiting.set()
                    if not release.wait(10): raise AssertionError('Fixture did not release B')
                return {'transactions': [{'id': 'purchase', 'accountId': key, 'date': '2026-10-01', 'amount': -10, 'currency': 'GBP', 'description': 'Synthetic shop'}]}
            return read
        def refresh(conn, boundary):
            if current_thread().name == 'failed-B': raise RuntimeError('Synthetic calculation failure')
            return original(conn, boundary)
        def run_b():
            try: results['B'] = routes.run_sync(reader=reader('B'), db_path=self.path, provider_connection_id='connection:B')
            except BaseException as error: failures.append(error)
        with patch.object(routes, 'refresh_derived', side_effect=refresh):
            worker = Thread(target=run_b, name='failed-B'); worker.start()
            try:
                self.assertTrue(waiting.wait(10))
                results['A'] = routes.run_sync(reader=reader('A'), db_path=self.path, provider_connection_id='connection:A')
                self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM bank_derived_refresh_work').fetchone()[0], 1)
            finally:
                release.set(); worker.join(15)
            self.assertFalse(worker.is_alive()); self.assertEqual(failures, [])
        self.assertIn('derived spending insights', results['B']['result']['error'])
        with self.conn: self.conn.execute('UPDATE bank_provider_sync_locks SET last_attempt_at=NULL')
        retried = routes.run_sync(reader=reader('B'), db_path=self.path, provider_connection_id='connection:B')
        self.assertIsNone(retried['result']['error'])
        self.assertEqual([row[0] for row in self.conn.execute('SELECT category FROM transactions')], ['Groceries', 'Groceries'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM bank_derived_refresh_work').fetchone()[0], 0)

    def test_upgrade_recovers_an_unfinished_legacy_singleton(self):
        with self.conn:
            self.conn.execute("INSERT INTO accounts(id,bank,account_type) VALUES('legacy','Synthetic','cash')")
            self.conn.execute("INSERT INTO transactions(account_id,txn_date,amount,description,type_code) VALUES('legacy','2026-07-01',-10,'Synthetic shop','bank_feed')")
            self.conn.execute('INSERT INTO bank_derived_refresh VALUES(1,0)')
        def reader(operation, account_id=None): return {'accounts': []}
        result = routes.run_sync(reader=reader, db_path=self.path)
        self.assertIsNone(result['result']['error'])
        self.assertIsNotNone(self.conn.execute('SELECT classification FROM transactions').fetchone()[0])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM bank_derived_refresh').fetchone()[0], 0)

    def test_legacy_schema_upgrade_preserves_evidence_and_active_link_uniqueness(self):
        local = self.original_card()
        original = [tuple(row) for row in self.conn.execute('SELECT * FROM bank_transactions')]
        with self.conn:
            self.conn.execute('DROP TABLE bank_connection_successors')
            self.conn.execute('DROP TABLE bank_derived_refresh_work')
            self.conn.execute('DROP INDEX ix_bank_transaction_link')
            self.conn.execute('ALTER TABLE bank_transactions DROP COLUMN historical')
            self.conn.execute('CREATE UNIQUE INDEX ix_bank_transaction_link ON bank_transactions(transaction_id) WHERE transaction_id IS NOT NULL')
            self.conn.execute('DROP INDEX ix_bank_local_mapping')
            self.conn.execute('CREATE UNIQUE INDEX ix_bank_local_mapping ON bank_connections(account_id) WHERE account_id IS NOT NULL')
        with closing(store.database(self.path)) as upgraded:
            self.assertEqual([tuple(row) for row in upgraded.execute('SELECT * FROM bank_transactions')], original)
            self.assertEqual(upgraded.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.discover(['new']); self.save('new')
        with self.conn:
            store.map_account(self.conn, 'new', account_id=local, balance_multiplier=-1,
                              replace_connection_id='old', confirm_reconnection=True)
        self.discover(['new', 'third']); self.save('third')
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError), self.conn:
            self.conn.execute("UPDATE bank_connections SET account_id=? WHERE id='third'", (local,))
        with self.assertRaises(sqlite3.IntegrityError), self.conn:
            self.conn.execute("UPDATE bank_transactions SET transaction_id=1 WHERE connection_id='third'")

    def test_paused_feed_blocks_automatic_duplicate_cash_accounts(self):
        self.discover(['old'], bank='HSBC', name='Current')
        self.save('old')
        stamp = datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute("INSERT INTO provider_connections(id,provider,label,secrets,enabled,status,created_at,updated_at) VALUES('connection:paused','lunchflow','Synthetic paused feed',NULL,0,'paused',?,?)", (stamp,stamp))
            self.conn.execute("UPDATE bank_connections SET provider_connection_id='connection:paused' WHERE id='old'")
        local = self.conn.execute('SELECT id FROM accounts').fetchone()[0]
        self.discover(['new'], bank='HSBC', name='Current')
        self.save('new')
        self.assertIsNone(self.conn.execute("SELECT account_id FROM bank_connections WHERE id='new'").fetchone()[0])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM accounts').fetchone()[0], 1)
        with self.conn:
            store.map_account(self.conn, 'new', account_id=local, replace_connection_id='old', confirm_reconnection=True)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 1)

    def test_invalid_successor_settings_roll_back_all_historical_changes(self):
        local = self.original_card()
        self.discover(['new']); self.save('new')
        before = list(self.conn.iterdump())
        with self.assertRaisesRegex(ValueError, 'balance direction'), self.conn:
            store.map_account(self.conn, 'new', account_id=local,
                              replace_connection_id='old', confirm_reconnection=True)
        self.assertEqual(list(self.conn.iterdump()), before)

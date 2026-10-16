"""Failed initialization closes its handle; successful callers retain ownership."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app import db, market_cache, workspace


class DatabaseConnectionLifetimeTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(folder) / 'synthetic.db'

    def assert_closed(self, connection):
        with self.assertRaisesRegex(sqlite3.ProgrammingError, 'closed'):
            connection.execute('SELECT 1')

    def test_invalid_ledger_schema_closes_failed_initialization(self):
        with closing(sqlite3.connect(self.path)) as original:
            original.execute('CREATE TABLE transactions(id INTEGER PRIMARY KEY, amount REAL)')
            original.commit()
        connection = db.connect(self.path)
        self.addCleanup(connection.close)
        with patch.object(db, 'connect', return_value=connection):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'txn_date'):
                db.init_db(self.path)
        self.assert_closed(connection)

    def test_newer_schema_refusal_closes_without_reinterpreting_ledger(self):
        connection = db.connect(self.path)
        self.addCleanup(connection.close)
        connection.execute(f'PRAGMA user_version={db.SCHEMA_VERSION + 1}')
        with patch.object(db, 'connect', return_value=connection):
            with self.assertRaisesRegex(RuntimeError, 'newer application release'):
                db.init_db(self.path)
        self.assert_closed(connection)
        with closing(sqlite3.connect(self.path)) as inspect:
            self.assertEqual(inspect.execute('PRAGMA user_version').fetchone()[0], db.SCHEMA_VERSION + 1)
            self.assertEqual(inspect.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0], 0)

    def test_readonly_market_schema_failure_closes_its_connection(self):
        with closing(sqlite3.connect(self.path)):
            pass
        connection = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)
        self.addCleanup(connection.close)
        with patch.object(db, 'connect', return_value=connection):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'readonly'):
                market_cache.database(self.path)
        self.assert_closed(connection)

    def test_readonly_workspace_schema_failure_closes_its_connection(self):
        with closing(db.init_db(self.path)):
            pass
        before = self.path.read_bytes()
        connection = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        with patch.object(db, 'DB_PATH', self.path), patch.object(db, 'connect', return_value=connection):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'readonly'):
                workspace._open()
        self.assert_closed(connection)
        self.assertEqual(self.path.read_bytes(), before)

    def test_successful_workspace_initialization_leaves_ownership_with_caller(self):
        with closing(db.init_db(self.path)):
            pass
        with patch.object(db, 'DB_PATH', self.path):
            connection = workspace._open()
        self.addCleanup(connection.close)
        self.assertEqual(workspace.preferences(connection), workspace.DEFAULTS)
        self.assertFalse(connection.in_transaction)
        connection.execute("INSERT INTO accounts(id,bank,account_type) VALUES('synthetic','Example','cash')")
        connection.commit()
        with closing(db.connect(self.path)) as inspect:
            self.assertEqual(inspect.execute("SELECT bank FROM accounts WHERE id='synthetic'").fetchone()[0], 'Example')
            self.assertEqual(workspace.preferences(inspect), workspace.DEFAULTS)

    def test_foreign_key_setup_failure_closes_new_connection(self):
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        connection.set_authorizer(lambda action, first, second, database, source:
                                  sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_PRAGMA and first == 'foreign_keys'
                                  else sqlite3.SQLITE_OK)
        with patch.object(db.sqlite3, 'connect', return_value=connection):
            with self.assertRaisesRegex(sqlite3.DatabaseError, 'authorized'):
                db.connect(self.path)
        self.assert_closed(connection)

    def test_wal_unavailable_keeps_memory_fallback_and_caller_ownership(self):
        class UnsupportedWalConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql == 'PRAGMA journal_mode=WAL':
                    raise sqlite3.OperationalError('Synthetic filesystem does not support WAL')
                return super().execute(sql, *args)
        connection = sqlite3.connect(self.path, factory=UnsupportedWalConnection)
        self.addCleanup(connection.close)
        with patch.object(db.sqlite3, 'connect', return_value=connection):
            opened = db.connect(self.path)
        self.assertIs(opened, connection)
        self.assertEqual(opened.execute('PRAGMA journal_mode').fetchone()[0], 'memory')
        self.assertEqual(opened.execute('PRAGMA foreign_keys').fetchone()[0], 1)

    def test_failed_wal_and_memory_setup_closes_new_connection(self):
        class UnsupportedJournalConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql.startswith('PRAGMA journal_mode='):
                    raise sqlite3.OperationalError('Synthetic journal setup failed')
                return super().execute(sql, *args)
        connection = sqlite3.connect(self.path, factory=UnsupportedJournalConnection)
        self.addCleanup(connection.close)
        with patch.object(db.sqlite3, 'connect', return_value=connection):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'journal setup failed'):
                db.connect(self.path)
        self.assert_closed(connection)

    def test_successful_ledger_and_market_connections_remain_usable(self):
        connection = db.init_db(self.path)
        self.addCleanup(connection.close)
        connection.execute("INSERT INTO accounts(id,bank,account_type) VALUES('synthetic','Example','cash')")
        connection.commit()
        market = market_cache.database(self.path)
        self.addCleanup(market.close)
        self.assertEqual(market.execute("SELECT bank FROM accounts WHERE id='synthetic'").fetchone()[0], 'Example')
        self.assertTrue(market.execute("SELECT 1 FROM sqlite_master WHERE name='investment_market_cache'").fetchone())


if __name__ == '__main__':
    unittest.main()

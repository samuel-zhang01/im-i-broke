"""Stopped WAL databases must be recoverable from read-only household mounts."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from app.db import init_db
from scripts.database import snapshot
from scripts.secure_backup import auth_snapshot
from scripts.verify_upgrade import verify


class OfflineDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='synthetic-stopped-database-')
        self.root = Path(self.temporary.name)
        self.data = self.root / 'readonly-data'
        self.data.mkdir()
        self.source = self.data / 'finance.db'
        with closing(init_db(self.source)) as conn:
            conn.execute("INSERT INTO accounts(id,bank,account_type) VALUES('synthetic','Synthetic bank','current')")
            conn.commit()

    def tearDown(self):
        self.data.chmod(0o700)
        for member in self.data.iterdir():
            if not member.is_symlink():
                member.chmod(0o600)
        self.temporary.cleanup()

    def freeze(self):
        before = {path.name: path.read_bytes() for path in self.data.iterdir()}
        for path in self.data.iterdir():
            path.chmod(0o400)
        self.data.chmod(0o500)
        return before

    def assert_unchanged(self, before):
        self.assertEqual({path.name: path.read_bytes() for path in self.data.iterdir()}, before)
        self.assertFalse(list(self.root.glob('.ledger-*')))

    def test_clean_shutdown_without_wal_sidecars_can_be_snapshotted_read_only(self):
        self.assertFalse(Path(str(self.source) + '-wal').exists())
        before = self.freeze()
        if os.geteuid() != 0:
            with self.assertRaisesRegex(sqlite3.OperationalError, 'readonly'):
                snapshot(self.source, self.root / 'normal.db')
        target = self.root / 'snapshot.db'
        result = snapshot(self.source, target, offline_source=True)
        self.assertEqual(result['rows']['accounts'], 1)
        with closing(sqlite3.connect(target)) as conn:
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
        self.assert_unchanged(before)

    def test_interrupted_writer_committed_wal_is_preserved_without_live_sidecar_writes(self):
        subprocess.run([sys.executable, '-c', '''
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute('PRAGMA wal_autocheckpoint=0')
conn.execute("INSERT INTO transactions(account_id,amount,dedupe_key) VALUES('synthetic',-42,'committed')")
conn.commit()
conn.execute("INSERT INTO transactions(account_id,amount,dedupe_key) VALUES('synthetic',-99,'uncommitted')")
os._exit(0)
''', str(self.source)], check=True)
        self.assertGreater(Path(str(self.source) + '-wal').stat().st_size, 0)
        # This demonstrates why immutable=1 would silently lose committed WAL.
        with closing(sqlite3.connect(self.source.as_uri() + '?immutable=1', uri=True)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 0)
        before = self.freeze()
        target = self.root / 'snapshot.db'
        snapshot(self.source, target, offline_source=True)
        with closing(sqlite3.connect(target)) as conn:
            self.assertEqual(conn.execute('SELECT amount,dedupe_key FROM transactions').fetchall(), [(-42, 'committed')])
        self.assert_unchanged(before)

    def test_authentication_copy_revokes_sessions_and_bootstrap_without_changing_source(self):
        source = self.data / 'auth.db'
        with closing(sqlite3.connect(source)) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('CREATE TABLE sessions(digest TEXT)')
            conn.execute('CREATE TABLE installation_setup(token_digest TEXT)')
            conn.execute("INSERT INTO sessions VALUES('synthetic-session')")
            conn.execute("INSERT INTO installation_setup VALUES('synthetic-bootstrap')")
            conn.commit()
        before = self.freeze()
        target = self.root / 'auth-snapshot.db'
        auth_snapshot(source, target, offline_source=True)
        with closing(sqlite3.connect(target)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM installation_setup').fetchone()[0], 0)
        self.assert_unchanged(before)

    def test_interrupted_rollback_journal_is_recovered_only_in_the_private_copy(self):
        with closing(sqlite3.connect(self.source)) as conn:
            conn.execute('PRAGMA journal_mode=DELETE')
            conn.execute('CREATE TABLE offline_probe(id INTEGER PRIMARY KEY, payload TEXT)')
            conn.executemany('INSERT INTO offline_probe(payload) VALUES(?)', [('original' * 600,)] * 80)
            conn.commit()
        subprocess.run([sys.executable, '-c', '''
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute('PRAGMA cache_size=5')
conn.execute('UPDATE offline_probe SET payload=?', ('uncommitted' * 500,))
os._exit(0)
''', str(self.source)], check=True)
        self.assertGreater(Path(str(self.source) + '-journal').stat().st_size, 0)
        before = self.freeze()
        target = self.root / 'snapshot.db'
        snapshot(self.source, target, offline_source=True)
        with closing(sqlite3.connect(target)) as conn:
            self.assertEqual(conn.execute('SELECT DISTINCT payload FROM offline_probe').fetchall(), [('original' * 600,)])
        self.assert_unchanged(before)

    def test_migration_rehearsal_works_on_read_only_stopped_database(self):
        before = self.freeze()
        result = verify(self.source, self.root, offline_source=True)
        self.assertTrue(result['ok'])
        self.assertEqual(result['protected_rows']['accounts'], 1)
        self.assert_unchanged(before)

    def test_invalid_database_copy_is_cleaned_without_publishing_a_snapshot(self):
        self.source.write_bytes(b'invalid synthetic database')
        before = self.freeze()
        target = self.root / 'snapshot.db'
        with self.assertRaises(sqlite3.DatabaseError):
            snapshot(self.source, target, offline_source=True)
        self.assertFalse(target.exists())
        self.assert_unchanged(before)

    def test_wal_symlink_is_refused_without_reading_its_target(self):
        external = self.root / 'external'
        external.write_bytes(b'synthetic unrelated file')
        Path(str(self.source) + '-wal').symlink_to(external)
        target = self.root / 'snapshot.db'
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            snapshot(self.source, target, offline_source=True)
        self.assertFalse(target.exists())
        self.assertFalse(list(self.root.glob('.ledger-*')))
        self.assertEqual(external.read_bytes(), b'synthetic unrelated file')

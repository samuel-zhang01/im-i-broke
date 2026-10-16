from contextlib import closing
import io
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from app.db import init_db
from scripts.secure_backup import create, restore
from scripts.initialize_storage import initialize


@unittest.skipUnless(shutil.which('age') and shutil.which('age-keygen'), 'age CLI required for encryption integration tests')
class EncryptedBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.data = self.folder / 'data'; self.data.mkdir()
        self.uploads = self.folder / 'uploads'; self.uploads.mkdir()
        conn = init_db(self.data / 'finance.db')
        conn.execute("INSERT INTO accounts(id,bank,account_type) VALUES('test','Synthetic bank','current')")
        conn.execute("INSERT INTO transactions(account_id,amount,dedupe_key) VALUES('test',-42,'test')")
        conn.commit(); conn.close()
        (self.uploads / 'statement.pdf').write_bytes(b'%PDF-synthetic-private-statement')
        self.key = self.folder / 'identity.txt'
        subprocess.run(['age-keygen', '-o', str(self.key)], check=True, capture_output=True)
        self.recipient = subprocess.check_output(['age-keygen', '-y', str(self.key)], text=True).strip()
        self.output = self.folder / 'backup.age'

    def tearDown(self):
        self.temp.cleanup()

    def test_round_trip_private_files_and_no_backup_key(self):
        with closing(sqlite3.connect(self.data / 'auth.db')) as conn, conn:
            conn.execute('CREATE TABLE sessions(digest TEXT)')
            conn.execute("INSERT INTO sessions VALUES('old-session')")
            conn.commit()
        (self.data / 'auth.key').write_text('secret-excluded-key')
        create(self.data, self.uploads, self.output, self.recipient, self.folder)
        self.assertNotIn(b'synthetic-private-statement', self.output.read_bytes())
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        restored = self.folder / 'restored'
        restore(self.output, self.key, restored, self.folder)
        self.assertFalse((restored / 'auth.key').exists())
        self.assertEqual((restored / 'uploads' / 'statement.pdf').read_bytes(), b'%PDF-synthetic-private-statement')
        self.assertEqual((restored / 'finance.db').stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(restored / 'finance.db')) as conn, conn:
            self.assertEqual(conn.execute('SELECT amount FROM transactions').fetchone()[0], -42)
        with closing(sqlite3.connect(restored / 'auth.db')) as conn, conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)
        with self.assertRaises(ValueError):
            create(self.data, self.uploads, self.output, self.recipient, self.folder)
        with self.assertRaises(ValueError):
            restore(self.output, self.key, restored, self.folder)

    def test_tamper_wrong_key_truncation_do_not_extract_anything(self):
        create(self.data, self.uploads, self.output, self.recipient, self.folder)
        original = self.output.read_bytes()
        alternate = self.folder / 'wrong-key'
        subprocess.run(['age-keygen', '-o', str(alternate)], check=True, capture_output=True)
        restored = self.folder / 'restored'
        with self.assertRaises(ValueError):
            restore(self.output, alternate, restored, self.folder)
        for damaged in [original[:-10], original[:-1] + bytes([original[-1] ^ 1])]:
            self.output.write_bytes(damaged)
            with self.assertRaises(ValueError):
                restore(self.output, self.key, restored, self.folder)
            self.assertFalse(restored.exists())

    def test_stopped_read_only_wal_databases_encrypt_restore_without_source_changes(self):
        with closing(sqlite3.connect(self.data / 'auth.db')) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('CREATE TABLE sessions(digest TEXT)')
            conn.execute("INSERT INTO sessions VALUES('synthetic-session')")
            conn.commit()
        before = {path.name: path.read_bytes() for path in self.data.iterdir()}
        for path in self.data.iterdir():
            path.chmod(0o400)
        self.data.chmod(0o500)
        try:
            create(self.data, self.uploads, self.output, self.recipient, self.folder, offline_source=True)
            self.assertEqual({path.name: path.read_bytes() for path in self.data.iterdir()}, before)
        finally:
            self.data.chmod(0o700)
            for path in self.data.iterdir():
                path.chmod(0o600)
        restored = self.folder / 'restored'
        restore(self.output, self.key, restored, self.folder)
        with closing(sqlite3.connect(restored / 'finance.db')) as conn:
            self.assertEqual(conn.execute('SELECT amount FROM transactions').fetchone()[0], -42)
        with closing(sqlite3.connect(restored / 'auth.db')) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)
        self.assertEqual((restored / 'uploads/statement.pdf').read_bytes(), b'%PDF-synthetic-private-statement')

    def test_symlinks_and_archive_traversal_are_rejected(self):
        (self.uploads / 'escape').symlink_to(self.key)
        with self.assertRaises(ValueError):
            create(self.data, self.uploads, self.output, self.recipient, self.folder)
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w') as tar:
            member = tarfile.TarInfo('../escape'); member.size = 3
            tar.addfile(member, io.BytesIO(b'bad'))
        encrypted = subprocess.check_output(['age', '-r', self.recipient], input=archive.getvalue())
        self.output.write_bytes(encrypted)
        with self.assertRaises(ValueError):
            restore(self.output, self.key, self.folder / 'restore', self.folder)
        self.assertFalse((self.folder / 'escape').exists())

    def test_manifest_fingerprints_key_without_archiving_it_and_revokes_bootstrap(self):
        from app.auth import SCHEMA
        with closing(sqlite3.connect(self.data / 'auth.db')) as conn, conn:
            conn.executescript(SCHEMA)
            conn.execute("INSERT INTO installation_setup(id,token_digest,expires) VALUES(1,'synthetic-token',9999999999)")
            conn.commit()
        auth_key = self.folder / 'auth.key'
        auth_key.write_bytes(b'synthetic-key-kept-separately')
        create(self.data, self.uploads, self.output, self.recipient, self.folder, auth_key)
        restored = self.folder / 'restored'
        restore(self.output, self.key, restored, self.folder)
        manifest = json.loads((restored / 'manifest.json').read_text())
        self.assertEqual(manifest['authentication_key_sha256'], hashlib.sha256(auth_key.read_bytes()).hexdigest())
        self.assertEqual(set(manifest['files']), {'finance.db', 'auth.db', 'uploads/statement.pdf'})
        self.assertFalse((restored / 'auth.key').exists())
        with closing(sqlite3.connect(restored / 'auth.db')) as conn, conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM installation_setup').fetchone()[0], 0)

    def test_authenticated_archive_with_bad_manifest_is_rejected_and_cleaned(self):
        create(self.data, self.uploads, self.output, self.recipient, self.folder)
        plain = subprocess.check_output(['age', '-d', '-i', str(self.key), str(self.output)])
        rewritten = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(plain), mode='r:') as original:
            with tarfile.open(fileobj=rewritten, mode='w') as modified:
                for member in original.getmembers():
                    content = original.extractfile(member).read()
                    if member.name == 'manifest.json':
                        manifest = json.loads(content)
                        manifest['files']['finance.db']['sha256'] = '0' * 64
                        content = json.dumps(manifest).encode()
                    member.size = len(content)
                    modified.addfile(member, io.BytesIO(content))
        self.output.write_bytes(subprocess.check_output(['age', '-r', self.recipient], input=rewritten.getvalue()))
        restored = self.folder / 'bad-restore'
        with self.assertRaisesRegex(ValueError, 'checksum'):
            restore(self.output, self.key, restored, self.folder)
        self.assertFalse(restored.exists())


class StorageMigrationTests(unittest.TestCase):
    def test_migration_moves_key_and_refuses_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            roots = [Path(folder) / name for name in ('data', 'uploads', 'keys')]
            for root in roots: root.mkdir()
            (roots[0] / 'auth.key').write_bytes(b'test-key')
            with patch('scripts.initialize_storage.os.chown'):
                initialize(roots)
            self.assertFalse((roots[0] / 'auth.key').exists())
            self.assertEqual((roots[2] / 'auth.key').read_bytes(), b'test-key')
            self.assertEqual((roots[2] / 'auth.key').stat().st_mode & 0o777, 0o600)
            (roots[1] / 'link').symlink_to(roots[0])
            with self.assertRaises(ValueError):
                initialize(roots)

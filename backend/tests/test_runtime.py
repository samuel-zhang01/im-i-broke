import tempfile
import sqlite3
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import init_db
from app.runtime import RequestBoundaryMiddleware, database_readiness, database_revision
from scripts.database import inspect, snapshot


class RuntimeBoundaryTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.add_middleware(RequestBoundaryMiddleware,
                           allowed_hosts=['localhost', '127.0.0.1', '192.0.2.10'])
        @app.post('/api/write')
        def write():
            return {'ok': True}
        @app.get('/api/read')
        def read():
            return {'ok': True}
        self.client = TestClient(app, base_url='http://127.0.0.1:8080')

    def test_local_lan_and_cli_requests_work_with_original_host_port(self):
        for host in ('127.0.0.1:8080', 'localhost:8080', '192.0.2.10:8080'):
            with self.subTest(host=host):
                self.assertEqual(self.client.post('/api/write', headers={'Host': host, 'Origin': 'http://' + host}).status_code, 200)
        self.assertEqual(self.client.post('/api/write').status_code, 200)
        self.assertEqual(self.client.post('/api/write', headers={'Origin': 'http://localhost:5173'}).status_code, 200)

    def test_unrelated_browser_origins_null_origin_and_rebinding_hosts_fail(self):
        for origin in ('https://unrelated.example', 'null', 'http://127.0.0.1:9999'):
            self.assertEqual(self.client.post('/api/write', headers={'Origin': origin}).status_code, 403)
        for host in ('unrelated.example', 'user@localhost:8080', 'localhost/path'):
            self.assertEqual(self.client.get('/api/read', headers={'Host': host}).status_code, 400)
        self.assertEqual(self.client.get('/api/read', headers={'Sec-Fetch-Site': 'cross-site'}).status_code, 403)

    def test_development_origin_exception_is_limited_to_local_mode(self):
        for mode in ('local', 'public', 'trusted-lan'):
            with self.subTest(mode=mode), patch.dict(os.environ, {
                    'FINANCE_DEPLOYMENT': mode, 'FINANCE_PUBLIC_ORIGIN': 'https://household.example.com'}):
                for origin in ('http://localhost:5173', 'http://127.0.0.1:5173'):
                    response = self.client.post('/api/write', headers={'Origin': origin})
                    self.assertEqual(response.status_code, 200 if mode == 'local' else 403)
                self.assertEqual(self.client.post('/api/write', headers={
                    'Origin': 'http://127.0.0.1:8080'}).status_code, 200)
                self.assertEqual(self.client.post('/api/write', headers={
                    'Origin': 'https://household.example.com'}).status_code, 200)
                self.assertEqual(self.client.post('/api/write').status_code, 200)


class DatabaseRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.source = self.folder / 'source.db'
        self.conn = init_db(self.source)
        self.conn.execute("INSERT INTO accounts(id,bank,account_type) VALUES('retained','Old joint source','current')")
        self.conn.execute("INSERT INTO transactions(account_id,txn_date,amount,dedupe_key) VALUES('retained','2020-01-01',-7,'retained')")
        self.conn.execute("INSERT INTO merchants(canonical_name,kind,source) VALUES('Private synthetic person','person','user')")
        self.conn.execute("INSERT INTO overrides(dedupe_key,category,note) VALUES('retained','Housing','Keep this review')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_backup_reads_committed_wal_and_restore_preserves_private_decisions(self):
        backup, restore = self.folder / 'backup.db', self.folder / 'restore.db'
        self.assertTrue(Path(str(self.source) + '-wal').exists())
        original = inspect(self.source)
        self.assertEqual(snapshot(self.source, backup), original)
        self.assertFalse(Path(str(backup) + '-wal').exists())
        self.assertFalse(Path(str(backup) + '-shm').exists())
        self.assertEqual(snapshot(backup, restore), original)
        restored = init_db(restore)
        self.assertEqual(tuple(restored.execute("SELECT id,amount,dedupe_key FROM transactions").fetchone()), (1, -7, 'retained'))
        self.assertEqual(restored.execute("SELECT note FROM overrides WHERE dedupe_key='retained'").fetchone()[0], 'Keep this review')
        self.assertEqual(restored.execute("SELECT COUNT(*) FROM merchants WHERE kind='person' AND source='user'").fetchone()[0], 1)
        restored.close()

    def test_failed_or_overwriting_restore_never_changes_destination(self):
        backup = self.folder / 'backup.db'
        snapshot(self.source, backup)
        original = backup.read_bytes()
        with self.assertRaises(ValueError):
            snapshot(self.source, backup)
        self.assertEqual(backup.read_bytes(), original)
        invalid = self.folder / 'invalid.db'
        invalid.write_text('not sqlite')
        with self.assertRaises(sqlite3.DatabaseError):
            snapshot(invalid, self.folder / 'failed.db')
        self.assertFalse((self.folder / 'failed.db').exists())
        link = self.folder / 'dangling-link.db'
        link.symlink_to(self.folder / 'unintended.db')
        with self.assertRaises(ValueError):
            snapshot(self.source, link)
        self.assertFalse((self.folder / 'unintended.db').exists())

    def test_readiness_rejects_missing_malformed_and_unrelated_database(self):
        self.assertEqual(database_readiness(self.source)[1], 200)
        self.assertEqual(database_readiness(self.folder / 'missing.db')[1], 503)
        invalid = self.folder / 'invalid.db'
        invalid.write_text('not sqlite')
        self.assertEqual(database_readiness(invalid)[1], 503)
        empty = self.folder / 'empty.db'
        empty.touch()
        self.assertEqual(database_readiness(empty)[1], 503)

    def test_revision_is_read_only_stable_and_changes_on_committed_wal_write(self):
        before = database_revision(self.source)
        for _ in range(3):
            database_readiness(self.source)
            self.assertEqual(database_revision(self.source), before)
        self.conn.execute("UPDATE overrides SET note='Changed on another client'")
        self.conn.commit()
        after = database_revision(self.source)
        self.assertNotEqual(after, before)
        self.assertEqual(database_revision(self.source), after)
        self.assertEqual(len(after[0]['revision']), 24)


if __name__ == '__main__':
    unittest.main()

from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import investment_read_cache as reads


class InvestmentReadCacheTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'synthetic.db'
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('CREATE TABLE fixture(value INTEGER)')
            conn.execute('INSERT INTO fixture VALUES(1)')
        self.cache = reads.PrivateReadCache()

    def value(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            return {'value': conn.execute('SELECT value FROM fixture').fetchone()[0]}

    def read(self, filters=()):
        return self.cache.read(self.path, 'analysis', filters, self.value)

    def test_repeat_reads_hit_and_every_other_connection_commit_invalidates(self):
        first, status = self.read()
        self.assertEqual(status, 'MISS')
        self.assertEqual(self.read(), (first, 'HIT'))
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('UPDATE fixture SET value=2')
        body, status = self.read()
        self.assertEqual(status, 'MISS')
        self.assertEqual(json.loads(body), {'value': 2})

    def test_file_replacement_and_missing_database_never_reuse_previous_payload(self):
        self.read()
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        replacement = self.path.with_name('replacement.db')
        with closing(sqlite3.connect(replacement)) as conn, conn:
            conn.execute('CREATE TABLE fixture(value INTEGER)')
            conn.execute('INSERT INTO fixture VALUES(9)')
        replacement.replace(self.path)
        self.assertEqual(json.loads(self.read()[0]), {'value': 9})
        self.path.unlink()
        body, status = self.cache.read(self.path, 'analysis', (), lambda: {'unavailable': True})
        self.assertEqual(status, 'BYPASS')
        self.assertEqual(json.loads(body), {'unavailable': True})

    def test_filters_scope_database_and_utc_day_are_separate(self):
        self.assertEqual(self.read(('GBP', '1m', None, False))[1], 'MISS')
        self.assertEqual(self.read(('USD', '1m', None, False))[1], 'MISS')
        self.assertEqual(self.read(('GBP', '1m', (), False))[1], 'MISS')
        self.assertEqual(self.cache.read(self.path, 'security', (), self.value)[1], 'MISS')
        other = self.path.with_name('other.db')
        with closing(sqlite3.connect(other)) as conn, conn:
            conn.execute('CREATE TABLE fixture(value INTEGER)')
        self.assertEqual(json.loads(self.cache.read(other, 'analysis', (), lambda: {'other_database': True})[0]),
                         {'other_database': True})
        with patch.object(reads, 'datetime') as clock:
            clock.now.return_value.date.return_value.isoformat.return_value = '2040-01-01'
            self.assertEqual(self.read()[1], 'MISS')
            clock.now.return_value.date.return_value.isoformat.return_value = '2040-01-02'
            self.assertEqual(self.read()[1], 'MISS')

    def test_revision_crossing_during_computation_and_failures_are_not_retained(self):
        def changing():
            value = self.value()
            with closing(sqlite3.connect(self.path)) as conn, conn:
                conn.execute('UPDATE fixture SET value=3')
            return value
        self.assertEqual(self.cache.read(self.path, 'analysis', (), changing)[1], 'BYPASS')
        self.assertEqual(json.loads(self.read()[0]), {'value': 3})
        def failure():
            raise ValueError('Synthetic unavailable observation')
        for _ in range(2):
            with self.assertRaises(ValueError):
                self.cache.read(self.path, 'failure', (), failure)
        self.assertFalse(self.cache._pending)

    def test_expiry_lru_byte_budget_and_oversize_bypass(self):
        now = [0]
        self.cache = reads.PrivateReadCache(ttl=5, max_entries=2, max_bytes=40,
                                           max_entry_bytes=20, clock=lambda: now[0])
        self.read(('one',)); self.read(('two',)); self.read(('three',))
        self.assertLessEqual(len(self.cache._entries), 2)
        self.assertLessEqual(self.cache._bytes, 40)
        self.assertEqual(self.read(('one',))[1], 'MISS')
        now[0] = 6
        self.assertEqual(self.read(('one',))[1], 'MISS')
        self.assertEqual(len(self.cache._entries), 1)
        self.assertEqual(self.cache.read(self.path, 'large', (), lambda: {'data': 'x' * 50})[1], 'BYPASS')
        self.assertLessEqual(self.cache._bytes, 40)

    def test_concurrent_identical_reads_share_one_computation(self):
        started, release = threading.Event(), threading.Event()
        calls = []
        def compute():
            calls.append(1)
            started.set()
            self.assertTrue(release.wait(2))
            return {'known_zero': 0}
        with ThreadPoolExecutor(max_workers=4) as pool:
            first = pool.submit(self.cache.read, self.path, 'analysis', (), compute)
            self.assertTrue(started.wait(2))
            others = [pool.submit(self.cache.read, self.path, 'analysis', (), compute) for _ in range(3)]
            release.set()
            result = [first.result()] + [future.result() for future in others]
        self.assertEqual(len(calls), 1)
        self.assertEqual({body for body, _ in result}, {b'{"known_zero":0}'})
        self.assertTrue(all(status in {'MISS', 'HIT', 'COALESCED'} for _, status in result))

    def test_diagnostics_are_private_no_store_and_contain_no_filter_values(self):
        with patch.object(reads, 'cache', self.cache):
            response = reads.cached_json(self.path, 'analysis', ('PRIVATE_ACCOUNT_ID',), self.value)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(response.headers['pragma'], 'no-cache')
        self.assertEqual(response.headers['x-ledger-investment-cache'], 'MISS')
        self.assertIn('investment_read;dur=', response.headers['server-timing'])
        self.assertNotIn('PRIVATE', str(dict(response.headers)))


if __name__ == '__main__':
    unittest.main()

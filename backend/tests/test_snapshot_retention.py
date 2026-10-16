import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app import snaptrade_store as store, snapshot_retention as retention
from test_snaptrade_cache import portfolio

NOW = datetime(2027, 9, 19, 12, tzinfo=timezone.utc)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'finance.db'

    def save(self, stamp, *, status='ok', truncated=False, account='synthetic-account'):
        data = portfolio(stamp)
        data['accounts'][0]['id'] = account
        data['accounts'][0]['positions']['status'] = status
        data['accounts'][0]['positions']['truncated'] = truncated
        if status == 'empty':
            data['accounts'][0]['positions']['data'] = []
        store.save_portfolio(data, self.path)
        with store.database(self.path) as c:
            return c.execute('SELECT id FROM snaptrade_snapshots WHERE retrieved_at=?', (stamp,)).fetchone()[0]

    def test_old_details_compact_daily_and_all_balances_and_cache_survive(self):
        early = self.save('2026-01-01T08:00:00Z')
        late = self.save('2026-01-01T20:00:00Z')
        boundary = self.save('2026-09-18T00:00:00Z')
        recent = self.save('2027-09-19T08:00:00Z')
        before = store.list_account_observations(self.path)
        cache = store.load_portfolio(self.path)
        preview = retention.compact(self.path, current=NOW)
        self.assertEqual(preview['positions_removed'], 1)
        self.assertIsNotNone(store.get_snapshot(early, self.path))
        result = retention.compact(self.path, current=NOW, apply=True)
        self.assertEqual(result['positions_removed'], 1)
        self.assertEqual(store.list_account_observations(self.path), before)
        self.assertEqual(store.load_portfolio(self.path), cache)
        self.assertIsNone(store.get_snapshot(early, self.path))
        self.assertEqual({r['id'] for r in store.list_snapshots(self.path)}, {late, boundary, recent})
        self.assertTrue(retention.compact(self.path, current=NOW, apply=True)['already_run'])
        self.assertEqual(retention.compact(self.path, current=NOW)['positions_removed'], 0)
        with store.database(self.path) as c:
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_last_failed_or_truncated_read_does_not_discard_complete_holdings(self):
        for status, truncated in [('error', False), ('ok', True)]:
            with self.subTest(status=status, truncated=truncated):
                self.path = Path(self.temp.name) / (status + '.db')
                obsolete = self.save('2026-01-01T07:00:00Z')
                complete = self.save('2026-01-01T08:00:00Z')
                final = self.save('2026-01-01T20:00:00Z', status=status, truncated=truncated)
                retention.compact(self.path, current=NOW, apply=True)
                self.assertIsNone(store.get_snapshot(obsolete, self.path))
                self.assertEqual(store.get_snapshot(complete, self.path)['accounts'][0]['positions']['status'], 'ok')
                self.assertIsNotNone(store.get_snapshot(final, self.path))

    def test_disconnected_account_and_empty_positions_are_retained(self):
        first = self.save('2026-01-01T07:00:00Z', account='old-account')
        empty = self.save('2026-01-01T20:00:00Z', status='empty')
        self.save('2026-09-19T08:00:00Z')
        retention.compact(self.path, current=NOW, apply=True)
        self.assertIsNotNone(store.get_snapshot(first, self.path))
        self.assertEqual(store.get_snapshot(empty, self.path)['accounts'][0]['positions']['data'], [])

    def test_days_are_utc_and_invalid_dates_are_not_pruned(self):
        old = self.save('2026-01-01T23:00:00Z')
        # Same UTC day, even though the local calendar says January 2.
        latest = self.save('2026-01-02T00:30:00+01:00')
        with store.database(self.path) as c:
            c.execute("INSERT INTO snaptrade_snapshots(retrieved_at,account_count) VALUES('invalid',0)")
            c.commit()
        retention.compact(self.path, current=NOW, apply=True)
        self.assertIsNone(store.get_snapshot(old, self.path))
        self.assertIsNotNone(store.get_snapshot(latest, self.path))
        self.assertEqual(len(store.list_snapshots(self.path)), 2)

    def test_delete_failure_rolls_back_entire_compaction(self):
        first = self.save('2026-01-01T07:00:00Z')
        self.save('2026-01-01T20:00:00Z')
        with store.database(self.path) as c:
            c.execute("CREATE TRIGGER prevent_delete BEFORE DELETE ON snaptrade_position_observations BEGIN SELECT RAISE(ABORT,'test failure'); END")
            c.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            retention.compact(self.path, current=NOW, apply=True)
        self.assertIsNotNone(store.get_snapshot(first, self.path))
        with store.database(self.path) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM snapshot_retention_runs').fetchone()[0], 0)


class RetentionSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduler_runs_and_cancels_without_provider_configuration(self):
        async def sleep(seconds):
            if seconds == 3600:
                raise asyncio.CancelledError
        async def in_thread(fn, **kw):
            return fn(**kw)
        with patch('app.db.DB_PATH') as path, patch.object(retention, 'compact') as compact, \
                patch.object(retention.asyncio, 'sleep', side_effect=sleep), \
                patch.object(retention.asyncio, 'to_thread', side_effect=in_thread):
            path.exists.return_value = True
            with self.assertRaises(asyncio.CancelledError):
                await retention._run()
            compact.assert_called_once_with(apply=True)

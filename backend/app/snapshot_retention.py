"""Daily compaction of old holdings; balance and transaction evidence is retained."""
from datetime import datetime, timedelta, timezone
import asyncio
from contextlib import asynccontextmanager, suppress

DETAIL_DAYS = 366  # A full year, including a leap day, of detailed intraday holdings.


def ensure_schema(conn):
    columns = {r[1] for r in conn.execute('PRAGMA table_info(snaptrade_snapshots)')}
    if 'holdings_retained' not in columns:
        conn.execute('ALTER TABLE snaptrade_snapshots ADD COLUMN holdings_retained INTEGER NOT NULL DEFAULT 1')
    conn.execute('''CREATE TABLE IF NOT EXISTS snapshot_retention_runs (
        day TEXT PRIMARY KEY, cutoff TEXT NOT NULL, snapshots_compacted INTEGER NOT NULL,
        positions_removed INTEGER NOT NULL)''')


def compact(path=None, *, current=None, apply=False):
    """Preview by default; apply atomically at most once per UTC day.

    Keep the last receipt of each old day and the last complete holdings receipt
    for each account that day. Keep whole snapshots rather than assembling a
    fictitious portfolio out of observations at different times. All balances,
    source clocks, receipt headers, activities and current cache remain intact.
    """
    from .snaptrade_store import database
    current = current or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError('Retention time must include a timezone')
    current = current.astimezone(timezone.utc)
    # Only compact whole UTC days, conservatively retaining the boundary day.
    cutoff = (current - timedelta(days=DETAIL_DAYS)).date().isoformat()
    day = current.date().isoformat()
    with database(path) as conn:
        conn.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        if apply and conn.execute('SELECT 1 FROM snapshot_retention_runs WHERE day=?', (day,)).fetchone():
            return {'day': day, 'cutoff': cutoff, 'already_run': True,
                    'snapshots_compacted': 0, 'positions_removed': 0}
        conn.execute('CREATE TEMP TABLE retention_keep(id INTEGER PRIMARY KEY)')
        conn.execute('''INSERT OR IGNORE INTO retention_keep
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY date(retrieved_at)
                    ORDER BY julianday(retrieved_at) DESC,id DESC) AS rank
                FROM snaptrade_snapshots WHERE holdings_retained=1
                  AND date(retrieved_at) < ?
            ) WHERE rank=1''', (cutoff,))
        conn.execute('''INSERT OR IGNORE INTO retention_keep
            SELECT snapshot_id FROM (
                SELECT a.snapshot_id, ROW_NUMBER() OVER (
                    PARTITION BY a.account_id,date(s.retrieved_at)
                    ORDER BY julianday(s.retrieved_at) DESC,s.id DESC) AS rank
                FROM snaptrade_account_observations a
                JOIN snaptrade_snapshots s ON s.id=a.snapshot_id
                WHERE s.holdings_retained=1 AND date(s.retrieved_at) < ?
                  AND a.holdings_truncated=0 AND (
                    a.holdings_status='empty' OR (a.holdings_status='ok' AND EXISTS (
                        SELECT 1 FROM snaptrade_position_observations p
                        WHERE p.snapshot_id=a.snapshot_id AND p.account_id=a.account_id)))
            ) WHERE rank=1''', (cutoff,))
        # Even if polling has been disabled for months, retain the current cache's
        # original detailed receipt. Malformed dates are never deletion candidates.
        conn.execute('''INSERT OR IGNORE INTO retention_keep
            SELECT s.id FROM snaptrade_snapshots s JOIN snaptrade_cache c
            ON s.retrieved_at=c.last_successful_refresh_at''')
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='provider_connection_cache'").fetchone():
            conn.execute('''INSERT OR IGNORE INTO retention_keep
                SELECT s.id FROM snaptrade_snapshots s JOIN provider_connection_cache c
                ON s.retrieved_at=c.last_successful_refresh_at''')
        conn.execute('CREATE TEMP TABLE retention_remove(id INTEGER PRIMARY KEY)')
        conn.execute('''INSERT INTO retention_remove SELECT id FROM snaptrade_snapshots
            WHERE holdings_retained=1 AND date(retrieved_at) < ?
              AND id NOT IN (SELECT id FROM retention_keep)''', (cutoff,))
        result = {'day': day, 'cutoff': cutoff, 'already_run': False,
                  'snapshots_compacted': conn.execute('SELECT count(*) FROM retention_remove').fetchone()[0],
                  'positions_removed': conn.execute('''SELECT count(*) FROM snaptrade_position_observations
                      WHERE snapshot_id IN (SELECT id FROM retention_remove)''').fetchone()[0]}
        if apply:
            conn.execute('DELETE FROM snaptrade_position_observations WHERE snapshot_id IN (SELECT id FROM retention_remove)')
            conn.execute('UPDATE snaptrade_snapshots SET holdings_retained=0 WHERE id IN (SELECT id FROM retention_remove)')
            conn.execute('INSERT INTO snapshot_retention_runs VALUES(?,?,?,?)',
                         (day, cutoff, result['snapshots_compacted'], result['positions_removed']))
            conn.commit()
        return result


async def _run():
    from .db import DB_PATH
    await asyncio.sleep(60)
    while True:
        if DB_PATH.exists():
            try:
                await asyncio.to_thread(compact, apply=True)
            except Exception:
                # A failed transaction rolls back; retry later without logging
                # private data or preventing ordinary collection.
                pass
        await asyncio.sleep(3600)


@asynccontextmanager
async def snapshot_retention():
    # Independent of broker credentials and polling enablement.
    task = asyncio.create_task(_run(), name='snapshot-retention')
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

"""Inspect or snapshot Ledger SQLite databases without overwriting a target.

Usage: python scripts/database.py inspect SOURCE
       python scripts/database.py backup SOURCE NEW_BACKUP
       python scripts/database.py restore BACKUP NEW_DATABASE

SQLite's online backup API includes committed WAL data. Restore deliberately
requires a new path/volume; recovery never destroys the previous installation.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time


@contextmanager
def snapshot_source(source: Path, *, offline_source=False, directory=None):
    """Stage a stopped database when its mount cannot accept SQLite sidecars.

    The caller must stop all writers before opting in. Copy the WAL as well as
    the database so committed, uncheckpointed records survive; SQLite rebuilds
    shared memory in the private writable copy. The normal online backup path
    remains unchanged and must be used when writers can still be running.
    """
    if not offline_source:
        yield source.resolve().as_uri() + '?mode=ro'
        return
    with tempfile.TemporaryDirectory(prefix='.ledger-stopped-source-', dir=directory) as temporary:
        staged = Path(temporary) / 'source.db'
        for suffix in ('', '-wal', '-journal'):
            member = Path(str(source) + suffix)
            if member.is_symlink():
                raise ValueError('Stopped database copies refuse symlinks.')
            if suffix and not member.exists():
                continue
            if not member.is_file():
                raise ValueError('Stopped database copies require regular files.')
            target = Path(str(staged) + suffix)
            with member.open('rb') as original, os.fdopen(
                    os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as copied:
                shutil.copyfileobj(original, copied)
        # A stopped process may leave a hot rollback journal. Recovery belongs
        # only to this copy, never to the original read-only household mount.
        yield staged.as_uri() + '?mode=rw'


def inspect(path: Path) -> dict:
    path = path.resolve()
    if not path.is_file():
        raise ValueError('Source database does not exist.')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=30)) as conn:
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Database integrity check failed.')
        if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise ValueError('Database foreign-key check failed.')
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {'accounts', 'statements', 'transactions'}
        if not required <= tables:
            raise ValueError('Source is not a Ledger database: core tables are missing.')
        counts = {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                  for name in ('accounts', 'statements', 'transactions', 'merchants',
                               'merchant_aliases', 'merchant_suggestion_decisions', 'rules',
                               'overrides', 'planning_scenarios', 'preferences', 'enrichment_cache',
                               'snaptrade_cache', 'snaptrade_snapshots', 'snaptrade_account_observations',
                               'snaptrade_position_observations', 'snaptrade_account_estimates',
                               'investment_market_cache', 'investment_market_closes',
                               'bank_connections', 'bank_transactions', 'bank_transaction_revisions',
                               'bank_balance_observations', 'bank_sync_runs', 'bank_review_decisions',
                               'wealth_source_links', 'wealth_source_link_events') if name in tables}
        return {'integrity': 'ok', 'foreign_key_errors': 0, 'rows': counts}


def snapshot(source: Path, target: Path, *, offline_source=False) -> dict:
    if offline_source and source.is_symlink():
        raise ValueError('Stopped database copies refuse symlinks.')
    source = source.resolve()
    if not source.is_file():
        raise ValueError('Source database does not exist.')
    if target.exists() or target.is_symlink():
        raise ValueError('Target already exists. Choose a new backup path or an empty recovery volume.')
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.ledger-snapshot-', suffix='.db', dir=target.parent)
    os.close(fd)
    temporary = Path(temporary)
    try:
        deadline = time.monotonic() + 60
        def progress(status, remaining, total):
            if time.monotonic() > deadline:
                raise TimeoutError('Snapshot exceeded 60 seconds; check database locks and available storage.')
        with snapshot_source(source, offline_source=offline_source, directory=target.parent) as uri, \
                closing(sqlite3.connect(uri, uri=True, timeout=30)) as src, \
                closing(sqlite3.connect(temporary)) as dst:
            src.backup(dst, pages=256, sleep=0.05, progress=progress)
            # A portable backup is one standalone file. Retaining WAL mode
            # can need writable sidecars on a read-only recovery mount.
            dst.execute('PRAGMA journal_mode=DELETE')
        result = inspect(temporary)
        with temporary.open('rb') as file:
            os.fsync(file.fileno())
        # Same-filesystem hard link is atomic and refuses a concurrently created
        # target. A failed copy never leaves a partial destination database.
        os.link(temporary, target)
        return result
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    check = sub.add_parser('inspect')
    check.add_argument('source', type=Path)
    for command in ('backup', 'restore'):
        copy = sub.add_parser(command)
        copy.add_argument('source', type=Path)
        copy.add_argument('target', type=Path)
    args = parser.parse_args()
    try:
        result = inspect(args.source) if args.command == 'inspect' else snapshot(args.source, args.target)
    except (ValueError, sqlite3.Error, OSError) as error:
        parser.exit(1, f'{args.command} failed: {error}\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()

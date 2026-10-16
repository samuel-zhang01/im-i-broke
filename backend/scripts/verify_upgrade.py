"""Rehearse migration and database backup/restore on a private temporary copy.

The source is opened read-only. Output contains row counts and check results,
never financial rows. Use a tmpfs or encrypted work directory for real data.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile

from app.db import init_db, apply_startup_migrations
from app.lunchflow_store import ensure_schema
from scripts.database import inspect, snapshot


def evidence(conn, columns=None):
    retained = {}
    selected = {}
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for name in ('accounts', 'statements', 'transactions', 'rules', 'overrides', 'planning_scenarios', 'preferences',
                 'anomaly_review_exceptions', 'anomaly_reviews',
                 'merchant_suggestion_decisions', 'merchant_merge_history', 'bank_review_decisions',
                 'bank_connections', 'bank_transactions', 'bank_transaction_revisions', 'bank_balance_observations',
                 'snaptrade_cache', 'snaptrade_snapshots', 'snaptrade_account_observations',
                 'snaptrade_position_observations', 'snaptrade_account_estimates',
                 'snaptrade_activity_accounts', 'snaptrade_activities', 'snaptrade_activity_fetches',
                 'wealth_source_links', 'wealth_source_link_events', 'workspace', 'category_catalog',
                 'category_names', 'workspace_changes', 'account_settings', 'account_settings_history',
                 'manual_balance_observations', 'account_coverage_periods', 'workspace_fx_rates',
                 'csv_import_templates', 'csv_import_batches', 'csv_import_rows',
                 'provider_connections', 'provider_connection_jobs', 'provider_connection_cache',
                 'provider_connection_subjects', 'native_transaction_reviews'):
        if name not in tables:
            continue
        available = [row[1] for row in conn.execute(f'PRAGMA table_info("{name}")')]
        if columns is not None:
            if name not in columns:
                continue
            fields = columns[name]
        elif name == 'transactions':
            fields = [field for field in ('id', 'account_id', 'statement_id', 'txn_date', 'description',
                      'amount', 'currency', 'balance', 'type_code', 'raw', 'meta', 'dedupe_key') if field in available]
        else:
            fields = available
        selected[name] = fields
        expressions = ['"' + field.replace('"', '""') + '"' for field in fields]
        if name == 'bank_connections' and 'external_id' in fields and 'provider_connection_id' in available:
            # The additive legacy-identity backfill explicitly copies the
            # original primary key; compare its equivalent meaning on both
            # sides without exempting changed provider identities.
            expressions[fields.index('external_id')] = "COALESCE(external_id,CASE WHEN provider_connection_id='environment:lunchflow' THEN id END)"
        sql_fields = ','.join(expressions)
        rows = sorted(json.dumps(list(row), ensure_ascii=False, default=str) for row in conn.execute(f'SELECT {sql_fields} FROM "{name}"'))
        retained[name] = {'rows': len(rows), 'sha256': hashlib.sha256('\n'.join(rows).encode()).hexdigest()}
    return retained, selected


def verify(source, work_dir=None, *, offline_source=False):
    with tempfile.TemporaryDirectory(prefix='ledger-upgrade-check-', dir=work_dir) as directory:
        target = Path(directory) / 'candidate.db'
        snapshot(source, target, offline_source=offline_source)
        with closing(sqlite3.connect(target)) as conn:
            before, columns = evidence(conn)
        conn = init_db(target)
        try:
            ensure_schema(conn)
            conn.commit()
            apply_startup_migrations(conn)
            after, _ = evidence(conn, columns)
            if before != after:
                changed = sorted(name for name in before if before[name] != after.get(name))
                raise ValueError('Migration changed protected source records or saved decisions in: ' + ', '.join(changed))
            if apply_startup_migrations(conn):
                raise ValueError('Startup migration repeated on an unchanged database.')
        finally:
            conn.close()
        integrity = inspect(target)
        backup, restored = Path(directory) / 'backup.db', Path(directory) / 'restored.db'
        snapshot(target, backup)
        snapshot(backup, restored)
        with closing(sqlite3.connect(restored)) as conn:
            restored_evidence, _ = evidence(conn, columns)
        if restored_evidence != before:
            raise ValueError('Restored protected records differ from the source.')
        return {'ok': True, 'source_modified': False, 'migration_repeat_noop': True,
                'backup_restore_verified': True, 'protected_rows': {name: value['rows'] for name, value in before.items()},
                'integrity': integrity['integrity']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--work-dir', type=Path)
    parser.add_argument('--offline-source', action='store_true',
                        help='All database writers are stopped: stage the database/WAL before reading read-only storage.')
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.source, args.work_dir, offline_source=args.offline_source), indent=2))
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f'Upgrade rehearsal failed: {error}\n')


if __name__ == '__main__':
    main()

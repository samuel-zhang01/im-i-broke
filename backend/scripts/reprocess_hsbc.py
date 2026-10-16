"""Preview existing HSBC date corrections on a copy; apply only explicitly.

Use the deployed backend image with provider networking disabled. Keep original
PDFs and the deployment recovery set. Applying requires all database writers
to be stopped; this command never rebuilds a household or refreshes a provider.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile

from app.db import DB_PATH, connect, init_db
from app.ingest import detect_bank, ingest_file
from scripts.database import inspect, snapshot
from scripts.verify_upgrade import evidence


class ReprocessError(ValueError):
    """Fixed operator diagnostics without source documents or financial rows."""


def _rows(conn):
    return {row['id']: dict(row) for row in conn.execute('SELECT * FROM transactions')}


def _repair(conn, files):
    before, _ = evidence(conn)
    original = _rows(conn)
    statement_facts = {row['id']: tuple(row)[1:] for row in conn.execute(
        'SELECT id,account_id,opening_balance,closing_balance FROM statements')}
    for file in files:
        if not file.is_file():
            raise ReprocessError('An input PDF is missing or unreadable.')
        folder, _ = detect_bank(str(file))
        if folder not in {'HSBC Debit', 'HSBC Credit'}:
            raise ReprocessError('Only HSBC statement PDFs can be reprocessed here.')
        result = ingest_file(str(file), conn=conn)
        if result['reconciled'] is not True:
            raise ReprocessError('A PDF does not reconcile with its printed totals. No live correction was applied.')
        if result['new_txns']:
            raise ReprocessError('A PDF would add purchases instead of correcting existing dates. Review its source and account mapping first.')
    current = _rows(conn)
    current_statement_facts = {row['id']: tuple(row)[1:] for row in conn.execute(
        'SELECT id,account_id,opening_balance,closing_balance FROM statements')}
    if any(current_statement_facts.get(ident) != facts for ident, facts in statement_facts.items()):
        raise ReprocessError('Reprocessing would change an existing statement account or printed balance. Review that financial correction separately.')
    if original.keys() != current.keys():
        raise ReprocessError('Reprocessing changed the set of ledger transactions.')
    financial = ('account_id', 'amount', 'currency', 'balance', 'type_code', 'raw', 'dedupe_key')
    for ident, old in original.items():
        if any(old[key] != current[ident][key] for key in financial):
            raise ReprocessError('Reprocessing would change a saved transaction beyond its dates and source evidence.')
        if old['statement_id'] is not None and old['statement_id'] != current[ident]['statement_id']:
            raise ReprocessError('Reprocessing would replace a saved statement assignment.')
    from app.categorize import apply_categorization
    from app.analytics import recompute_all
    apply_categorization(conn)
    recompute_all(conn, commit=False)
    current = _rows(conn)
    reviewed = ('merchant', 'category', 'category_source', 'merchant_kind', 'spending_treatment', 'exclude_from_analysis')
    for ident, old in original.items():
        if old['category_source'] == 'user' and any(old[key] != current[ident][key] for key in reviewed):
            raise ReprocessError('Reprocessing would change a reviewed transaction.')
    after, _ = evidence(conn)
    protected = set(before) - {'statements', 'transactions'}
    if any(before[name] != after.get(name) for name in protected):
        raise ReprocessError('Reprocessing would change protected accounts, provider evidence or saved decisions.')
    if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise ReprocessError('The corrected copy has a foreign-key error.')
    if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise ReprocessError('The corrected copy failed its integrity check.')
    return [{'transaction_id': ident, 'previous_date': old['txn_date'], 'corrected_date': current[ident]['txn_date']}
            for ident, old in original.items() if old['txn_date'] != current[ident]['txn_date']]


def reprocess(source, files, *, apply=False, offline_source=False, work_dir=None):
    source, files = Path(source), [Path(file) for file in files]
    if not files:
        raise ReprocessError('Select at least one original HSBC PDF.')
    if apply and not offline_source:
        raise ReprocessError('Stop all database writers, then supply --offline-source with --apply.')
    def file_hashes():
        result = []
        for file in files:
            with file.open('rb') as original:
                result.append(hashlib.file_digest(original, 'sha256').hexdigest())
        return result
    original_hashes = file_hashes()
    with tempfile.TemporaryDirectory(prefix='ledger-hsbc-preview-', dir=work_dir) as directory:
        candidate = Path(directory) / 'candidate.db'
        snapshot(source, candidate, offline_source=offline_source)
        with closing(init_db(candidate)) as conn:
            baseline, _ = evidence(conn)
            with conn:
                changes = _repair(conn, files)
        inspect(candidate)
        if file_hashes() != original_hashes:
            raise ReprocessError('An original PDF changed during the preview.')
        if apply:
            with closing(connect(source)) as live, live:
                live.execute('BEGIN IMMEDIATE')
                actual, _ = evidence(live)
                if actual != baseline:
                    raise ReprocessError('The live database changed or needs deployment migrations. Re-run the preview after stopping writers.')
                applied = _repair(live, files)
                if applied != changes or file_hashes() != original_hashes:
                    raise ReprocessError('The PDFs or database changed after the preview. No live correction was applied.')
    return {'mode': 'applied' if apply else 'preview', 'source_modified': apply,
            'pdfs_checked': len(files), 'changed_transaction_dates': len(changes),
            'new_transactions': 0, 'reviews_preserved': True, 'integrity': 'ok',
            'changes': changes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdfs', type=Path, nargs='+', help='Original HSBC PDFs accessible inside this container')
    parser.add_argument('--database', type=Path, default=DB_PATH)
    parser.add_argument('--apply', action='store_true', help='Apply the previewed date corrections to the existing database')
    parser.add_argument('--offline-source', action='store_true', help='All database writers are stopped; include committed journals in a staged copy')
    parser.add_argument('--work-dir', type=Path, help='Private tmpfs or encrypted directory for a larger preview')
    args = parser.parse_args()
    try:
        result = reprocess(args.database, args.pdfs, apply=args.apply,
                           offline_source=args.offline_source, work_dir=args.work_dir)
    except ReprocessError as error:
        parser.exit(1, f'HSBC reprocessing stopped: {error} No live correction was committed.\n')
    except (OSError, ValueError, sqlite3.Error):
        # Parser errors may contain merchant/file text. Keep diagnostics local
        # and bounded; detailed source inspection belongs in a private copy.
        parser.exit(1, 'HSBC reprocessing stopped. Check the PDF paths, printed totals, original source/account mapping and whether writers are stopped. No live correction was committed.\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()

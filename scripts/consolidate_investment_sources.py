#!/usr/bin/env python3
"""Dry run, then apply an exact reviewed CSV/API identity reconciliation.

python scripts/consolidate_investment_sources.py --db snapshot.db --data-root Data/Trading212 --manifest proof.json
python scripts/consolidate_investment_sources.py --db snapshot.db --data-root Data/Trading212 --manifest proof.json --apply

--apply revalidates all original source bytes, holdings and activity in a write
transaction. It records a reversible alias and one audit event; source rows,
API caches, balances, transactions and source documents are untouched.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'backend' if (root / 'backend').is_dir() else root))
from app.investment_source_consolidation import build_manifest, apply_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--account', default='Trading212-invest')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    mode = 'rw' if args.apply else 'ro'
    conn = sqlite3.connect(f'file:{args.db.resolve()}?mode={mode}', uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        if args.apply:
            manifest = json.loads(args.manifest.read_text())
            changed = apply_manifest(conn, manifest, data_root=args.data_root)
            result = {'status': 'linked' if changed else 'already_linked',
                      'source_id': manifest['source_id'], 'canonical_id': manifest['canonical_id'],
                      'evidence_digest': manifest['evidence_digest']}
        else:
            manifest = build_manifest(conn, source_id=args.account, data_root=args.data_root)
            args.manifest.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(args.manifest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
            result = {'status': 'dry_run', 'source_id': manifest['source_id'], 'canonical_id': manifest['canonical_id'],
                      'matching_events': manifest['matching_event_count'], 'holdings': manifest['holdings_count'],
                      'verified_executions': manifest['cash_verified_execution_count'],
                      'manifest': str(args.manifest), 'evidence_digest': manifest['evidence_digest']}
        print(json.dumps(result, indent=2))
    finally:
        conn.close()


if __name__ == '__main__':
    main()

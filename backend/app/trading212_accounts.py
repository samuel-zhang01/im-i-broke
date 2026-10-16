"""Explicit account routing for CSV exports that do not print an account ID."""
from __future__ import annotations

import json
from pathlib import Path

ACCOUNTS = {
    'stocks': ('Trading212-invest', 'Trading 212 · Stocks'),
    'isa': ('Trading212-isa', 'Trading 212 · Stocks & Shares ISA'),
}


def assign(statement, kind, source='user_selection'):
    if kind not in ACCOUNTS:
        raise ValueError('Choose Stocks or Stocks & Shares ISA for this Trading 212 CSV.')
    statement.account_id, label = ACCOUNTS[kind]
    statement.meta = {**(statement.meta or {}), 'parser': 'trading212',
                      'account_kind': kind, 'account_label': label,
                      'account_assignment_source': source}
    return statement


def folder_kind(path):
    """Only the documented provider/account folder layout establishes routing."""
    path = Path(path)
    if path.parent.parent.name == 'Trading212':
        return {'ISA': 'isa', 'Stocks': 'stocks'}.get(path.parent.name)
    return None


def saved_kind(conn, source_hash):
    """Resolve a byte-identical saved export, including content-addressed uploads."""
    found = set()
    for row in conn.execute("SELECT s.meta,s.account_id FROM statements s JOIN accounts a "
                            "ON a.id=s.account_id WHERE a.bank='Trading212'"):
        if json.loads(row['meta'] or '{}').get('source_sha256') == source_hash:
            found.add(row['account_id'])
    if len(found) != 1:
        return None
    account = next(iter(found))
    return next((kind for kind, (key, _) in ACCOUNTS.items() if key == account), None)


def resolve(conn, statement, path, source_hash):
    """Reuse saved source identity; never guess by ticker, currency or row count.

    A first legacy single-account installation retains its old default. Once
    multiple Trading 212 accounts exist, a new unidentified export needs an
    explicit selection. Identical provider IDs in different accounts are not
    evidence for merging those accounts.
    """
    explicit = (statement.meta or {}).get('account_kind')
    known = set()
    provenance = {}
    for row in conn.execute("SELECT s.source_file,s.meta,s.account_id FROM statements s "
                            "JOIN accounts a ON a.id=s.account_id WHERE a.bank='Trading212'"):
        meta = json.loads(row['meta'] or '{}')
        if str(Path(row['source_file']).resolve()) == str(Path(path).resolve()) or (
                source_hash and meta.get('source_sha256') == source_hash):
            known.add(row['account_id'])
            provenance[row['account_id']] = meta.get('account_assignment_source') or 'saved_source'
    if len(known) > 1:
        raise ValueError('This Trading 212 source is assigned to multiple accounts. Review its saved source records before importing.')
    if explicit:
        if known and statement.account_id not in known:
            raise ValueError('This Trading 212 file is already assigned to another account. Choose its saved account; no records were moved.')
        return statement
    if known:
        account = next(iter(known))
        for kind, (account_id, _) in ACCOUNTS.items():
            if account == account_id:
                return assign(statement, kind, provenance[account])
        raise ValueError('The saved Trading 212 account is unsupported. Review its account assignment before importing.')
    existing = [r['id'] for r in conn.execute("SELECT id FROM accounts WHERE bank='Trading212' AND account_type='investment'")]
    if len(existing) > 1:
        raise ValueError('This Trading 212 CSV does not identify its account. Choose Stocks or Stocks & Shares ISA in the import selector, then retry.')
    if existing == [ACCOUNTS['isa'][0]]:
        return assign(statement, 'isa', 'single_account')
    # Preserve the established ID and behavior for older single-account imports.
    return assign(statement, 'stocks', 'legacy_single_account')

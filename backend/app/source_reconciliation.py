"""Reversible, evidence-backed aliases for sources representing one account.

Source documents, transactions, provider caches and observations stay intact.
These links affect only account identity and aggregation. Institution names,
masked suffixes and similar holdings never establish a link on their own.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re


SCHEMA = '''
CREATE TABLE IF NOT EXISTS wealth_source_links (
    source_id TEXT PRIMARY KEY,
    canonical_id TEXT NOT NULL,
    evidence_kind TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    CHECK(source_id != canonical_id)
);
CREATE TABLE IF NOT EXISTS wealth_source_link_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    canonical_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('link','unlink')),
    evidence_kind TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
'''
EVIDENCE_KINDS = {'owner_confirmed', 'verified_provider_account_id', 'verified_statement_account_id', 'explicit_provider_mapping',
                  'verified_activity_reconciliation'}

ARCHIVE_SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS wealth_source_preferences (
        source_id TEXT PRIMARY KEY,
        archived INTEGER NOT NULL CHECK(archived IN (0,1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )''',
    '''CREATE TABLE IF NOT EXISTS wealth_source_preference_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id TEXT NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('archive','restore')),
        recorded_at TEXT NOT NULL
    )''',
)


def ensure_schema(conn):
    conn.executescript(SCHEMA)


def list_links(conn, *, include_inactive=False):
    # Read paths do not perform a migration or implicitly commit another
    # caller's work. The table is created by an explicit linking operation.
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wealth_source_links'").fetchone()
    if not exists:
        return []
    suffix = '' if include_inactive else ' WHERE active=1'
    return [dict(row) for row in conn.execute('SELECT * FROM wealth_source_links' + suffix + ' ORDER BY source_id')]


def resolve(source_id, mapping):
    seen = set()
    current = source_id
    while current in mapping:
        if current in seen:
            raise ValueError('Source links cannot contain a cycle.')
        seen.add(current)
        current = mapping[current]
    return current


def set_link(conn, *, source_id, canonical_id, evidence_kind, evidence_digest, accounts):
    """Explicit administrative operation; callers establish identity evidence.

    No automatic matching happens here. A digest references the separately
    reviewed evidence without storing full provider identifiers or documents.
    """
    lookup = {a['id']: a for a in accounts}
    if source_id == canonical_id or source_id not in lookup or canonical_id not in lookup:
        raise ValueError('Choose two distinct existing account sources.')
    if evidence_kind not in EVIDENCE_KINDS or not isinstance(evidence_digest, str) or not re.fullmatch(r'[a-f0-9]{64}', evidence_digest):
        raise ValueError('A supported identity evidence type and SHA-256 evidence digest are required.')
    first, second = lookup[source_id], lookup[canonical_id]
    if first.get('kind') != second.get('kind') or bool(first.get('is_paper')) != bool(second.get('is_paper')):
        raise ValueError('Linked sources must represent the same account kind and live/paper mode.')
    mapping = {r['source_id']: r['canonical_id'] for r in list_links(conn)}
    mapping[source_id] = canonical_id
    for identifier in mapping:
        resolve(identifier, mapping)
    ensure_schema(conn)
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute('''INSERT INTO wealth_source_links VALUES(?,?,?,?,?,?,1)
            ON CONFLICT(source_id) DO UPDATE SET canonical_id=excluded.canonical_id,
            evidence_kind=excluded.evidence_kind,evidence_digest=excluded.evidence_digest,
            updated_at=excluded.updated_at,active=1''', (source_id, canonical_id, evidence_kind, evidence_digest, now, now))
        conn.execute('INSERT INTO wealth_source_link_events(source_id,canonical_id,action,evidence_kind,evidence_digest,recorded_at) VALUES(?,?,?,?,?,?)',
                     (source_id, canonical_id, 'link', evidence_kind, evidence_digest, now))


def remove_link(conn, source_id):
    links = {row['source_id']: row for row in list_links(conn)}
    old = links.get(source_id)
    if old is None:
        return False
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute('UPDATE wealth_source_links SET active=0,updated_at=? WHERE source_id=?', (now, source_id))
        conn.execute('INSERT INTO wealth_source_link_events(source_id,canonical_id,action,evidence_kind,evidence_digest,recorded_at) VALUES(?,?,?,?,?,?)',
                     (source_id, old['canonical_id'], 'unlink', old['evidence_kind'], old['evidence_digest'], now))
    return True


def list_archive_preferences(conn):
    """Read user choices without migrations or writes on a wealth read."""
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wealth_source_preferences'").fetchone()
    if not exists:
        return []
    return [{**dict(row), 'archived': bool(row['archived'])} for row in conn.execute(
        'SELECT source_id,archived,created_at,updated_at FROM wealth_source_preferences ORDER BY source_id')]


def set_archived(conn, *, source_id, archived, accounts):
    """Hide an exact investment source by default without deleting its evidence.

    This is a display/selection preference, not an assertion that the account
    is closed or duplicates another source. Explicit selections can still
    inspect saved balances and history. Linked aliases retain their identity.
    """
    account = next((row for row in accounts if row['id'] == source_id), None)
    if account is None or account.get('kind') != 'investment':
        raise ValueError('Choose an existing investment account source to archive or restore.')
    if not isinstance(archived, bool):
        raise ValueError('Choose whether this investment source is archived.')
    previous = next((row for row in list_archive_preferences(conn) if row['source_id'] == source_id), None)
    if archived == bool(previous and previous['archived']):
        return False
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        for statement in ARCHIVE_SCHEMA:
            conn.execute(statement)
        conn.execute('''INSERT INTO wealth_source_preferences(source_id,archived,created_at,updated_at)
            VALUES(?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET
            archived=excluded.archived,updated_at=excluded.updated_at''',
                     (source_id, int(archived), now, now))
        conn.execute('INSERT INTO wealth_source_preference_events(source_id,action,recorded_at) VALUES(?,?,?)',
                     (source_id, 'archive' if archived else 'restore', now))
    return True


def apply_archive_preferences(accounts, preferences):
    """Apply after source links, retaining their history and canonical mapping."""
    archived = {row['source_id'] for row in preferences if row.get('archived')}
    for account in accounts:
        account['archived_by_user'] = account['kind'] == 'investment' and account['id'] in archived
        if account['archived_by_user']:
            reason = 'Archived from default net worth. Saved balances, transactions and historical observations are retained.'
            if account.get('exclusion_reason'):
                reason += ' ' + account['exclusion_reason']
            account.update(default_included=False, exclusion_reason=reason)
    return accounts


def apply_links(accounts, links):
    """Project aliases and extend canonical history, without deleting sources."""
    lookup = {a['id']: a for a in accounts}
    mapping = {row['source_id']: row['canonical_id'] for row in links
               if row.get('active', 1) and row['source_id'] in lookup and row['canonical_id'] in lookup}
    # Fail closed for corrupt/manual cycles: preserve separate accounts rather
    # than merging an ambiguous component or hiding its balance.
    resolved = {}
    for identifier in lookup:
        try:
            target = resolve(identifier, mapping)
        except ValueError:
            target = identifier
        if lookup[identifier].get('kind') != lookup[target].get('kind') or bool(lookup[identifier].get('is_paper')) != bool(lookup[target].get('is_paper')):
            target = identifier
        resolved[identifier] = target
    for account in accounts:
        canonical_id = resolved[account['id']]
        account.update(canonical_id=canonical_id, source_ids=[account['id']],
                       linked_to=canonical_id if canonical_id != account['id'] else None,
                       source_status='linked' if canonical_id != account['id'] else 'active')
        if canonical_id != account['id']:
            account.update(default_included=False,
                           exclusion_reason='Linked to its verified canonical account. Original records and historical observations are retained.')
    priority = {'snaptrade_estimate': 0, 'statement': 1, 'lunchflow': 2, 'recorded': 2}
    for canonical in accounts:
        if canonical['source_status'] != 'active':
            continue
        members = [a for a in accounts if resolved[a['id']] == canonical['id']]
        if len(members) < 2:
            continue
        canonical['source_ids'] = [a['id'] for a in members]
        points = {}
        for member in members:
            for item in member['observations']:
                rank = (priority.get(item.get('source'), 0) if item.get('reconciled', True) else -1,
                        member['id'] == canonical['id'])
                old = points.get(item['date'])
                if old is None or rank > old[0]:
                    points[item['date']] = (rank, item)
                elif rank == old[0] and (old[1]['value'], old[1]['currency']) != (item['value'], item['currency']):
                    points[item['date']] = (rank, {**item, 'value': None, 'reconciled': False})
        canonical['observations'] = [points[when][1] for when in sorted(points)]
    return accounts

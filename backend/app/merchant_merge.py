"""Confirmed merges of saved identities, retaining exact scopes and an audit trail.

Similarity is an invitation to review, never a classifier. A merge moves the
existing aliases only; source records stay disabled so seeds cannot resurrect
them. Preview classification runs in a disposable database, without ledger writes.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from . import analytics, merchants
from .categorize import apply_categorization


router = APIRouter(prefix='/api/merchants', tags=['merchants'])


class MergeRequest(BaseModel):
    target_id: int = Field(ge=1)
    source_ids: list[Annotated[int, Field(ge=1)]] = Field(min_length=1, max_length=20)
    preview_token: str | None = Field(default=None, max_length=64)


_TABLES = ('accounts', 'merchants', 'merchant_aliases', 'merchant_alias_approvals', 'merchant_alias_sources',
           'merchant_suggestion_decisions', 'transactions', 'overrides', 'rules')
_CHANGE_FIELDS = ('merchant', 'category', 'merchant_kind', 'spending_treatment',
                  'exclude_from_analysis', 'classification')


def _state(conn):
    # Include competing rules, reviews and newly imported transactions in the
    # freshness token, not just the selected merchants' last-modified times.
    return {name: [dict(row) for row in conn.execute(f'SELECT * FROM {name} ORDER BY rowid')]
            for name in _TABLES}


def _copy_for_preview(conn, state):
    snapshot = sqlite3.connect(':memory:')
    snapshot.row_factory = sqlite3.Row
    try:
        for name, rows in state.items():
            schema = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()[0]
            snapshot.execute(schema)
            if rows:
                columns = ','.join('"' + column + '"' for column in rows[0])
                marks = ','.join('?' for _ in rows[0])
                snapshot.executemany(f'INSERT INTO {name}({columns}) VALUES({marks})',
                                     [tuple(row.values()) for row in rows])
        return snapshot
    except Exception:
        snapshot.close()
        raise


def _selection(conn, body):
    source_ids = sorted(body.source_ids)
    if len(set(source_ids)) != len(source_ids) or body.target_id in source_ids:
        raise ValueError('Choose different saved names to merge and keep the target out of the source list.')
    items = {item['id']: item for item in merchants.list_merchants(conn)}
    if any(ident not in items for ident in [body.target_id, *source_ids]):
        raise merchants.AliasConflict('A selected name is no longer available. Refresh the directory before merging.')
    target, sources = items[body.target_id], [items[ident] for ident in source_ids]
    if any(source['kind'] != target['kind'] for source in sources):
        raise ValueError('Only names with the same type can be merged. Review their types first.')
    if any(source['spending_treatment'] != target['spending_treatment'] for source in sources):
        raise ValueError('Only names with the same spending treatment can be merged. Review their treatment first.')
    return target, sources


def _alias_key(alias):
    return merchants.key(alias['pattern']), alias['match_mode'], alias['context']


def _merged_aliases(target, sources, prepared):
    grouped = {}
    for merchant in [target, *sources]:
        for alias in merchant['aliases']:
            grouped.setdefault(_alias_key(alias), []).append(alias)
    rank = {alias['id']: index for index, alias in enumerate(prepared)}
    # Identical aliases can straddle another saved identity in the classifier's
    # ID tiebreak. Retaining the original winner preserves future ownership even
    # when no matching payment has been imported yet.
    return [sorted(aliases, key=lambda alias: rank[alias['id']]) for aliases in grouped.values()]


def _move_rules(conn, target, sources, alias_groups):
    """Apply only rule mutations; caller owns the transaction and categorization."""
    approvals = {row['alias_id']: row['approved_at'] for row in conn.execute('SELECT * FROM merchant_alias_approvals')}
    alias_sources = {row['id']: row['source'] for row in conn.execute('''
        SELECT a.id,COALESCE(s.source,m.source) AS source FROM merchant_aliases a
        JOIN merchants m ON m.id=a.merchant_id LEFT JOIN merchant_alias_sources s ON s.alias_id=a.id''')}
    for aliases in alias_groups:
        kept, *duplicates = aliases
        # Delete colliding target aliases before reparenting the original winner.
        # Keep approvals and source priority separately: an explicit approval
        # must still override the inherited public/account-evidence source.
        approved_at = [approvals[alias['id']] for alias in aliases if alias['id'] in approvals]
        for duplicate in duplicates:
            conn.execute('DELETE FROM merchant_alias_approvals WHERE alias_id=?', (duplicate['id'],))
            conn.execute('DELETE FROM merchant_alias_sources WHERE alias_id=?', (duplicate['id'],))
            conn.execute('DELETE FROM merchant_aliases WHERE id=?', (duplicate['id'],))
        conn.execute('UPDATE merchant_aliases SET merchant_id=? WHERE id=?', (target['id'], kept['id']))
        conn.execute('INSERT INTO merchant_alias_sources(alias_id,source) VALUES(?,?) ON CONFLICT(alias_id) DO UPDATE SET source=excluded.source',
                     (kept['id'], alias_sources[kept['id']]))
        if approved_at:
            conn.execute('INSERT INTO merchant_alias_approvals(alias_id,approved_at) VALUES(?,?) ON CONFLICT(alias_id) DO UPDATE SET approved_at=min(approved_at,excluded.approved_at)',
                         (kept['id'], min(approved_at)))
    # Consolidate decisions. Approval wins over dismissal when a retained alias
    # has already been approved; both original decisions remain in the audit.
    for source in sources:
        for decision in conn.execute('SELECT * FROM merchant_suggestion_decisions WHERE merchant_id=?', (source['id'],)).fetchall():
            conn.execute('''INSERT INTO merchant_suggestion_decisions(label_key,merchant_id,source_label,status,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(label_key,merchant_id) DO UPDATE SET
                status=CASE WHEN merchant_suggestion_decisions.status='approved' OR excluded.status='approved'
                            THEN 'approved' ELSE 'dismissed' END,
                updated_at=max(merchant_suggestion_decisions.updated_at,excluded.updated_at)''',
                (decision['label_key'], target['id'], decision['source_label'], decision['status'], decision['updated_at']))
        conn.execute('DELETE FROM merchant_suggestion_decisions WHERE merchant_id=?', (source['id'],))
        conn.execute('UPDATE merchants SET enabled=0,updated_at=CURRENT_TIMESTAMP WHERE id=?', (source['id'],))
    conn.execute('UPDATE merchants SET updated_at=CURRENT_TIMESTAMP WHERE id=?', (target['id'],))


def _categorize_preserving_source(conn, transaction_ids):
    # The general categorizer fills legacy parser-label gaps and may recover a
    # label from metadata. A name merge changes derived identity only: preserve
    # the stored source field byte-for-byte, including legacy NULLs.
    original = {row['id']: row['source_merchant'] for row in conn.execute('SELECT id,source_merchant FROM transactions')
                if row['id'] in transaction_ids}
    apply_categorization(conn, transaction_ids=transaction_ids)
    conn.executemany('UPDATE transactions SET source_merchant=? WHERE id=?',
                     [(original[row['id']], row['id']) for row in conn.execute('SELECT id,source_merchant FROM transactions')
                      if row['id'] in original and row['source_merchant'] != original[row['id']]])


def _prepare(conn, body):
    target, sources = _selection(conn, body)
    prepared = merchants._prepared(conn)
    alias_groups = _merged_aliases(target, sources, prepared)
    if not alias_groups:
        raise ValueError('Choose at least one saved name with matching aliases before merging.')
    if len(alias_groups) > merchants.MAX_ALIASES_PER_MERCHANT:
        raise ValueError(f'This merge would contain {len(alias_groups)} aliases; a saved name supports up to '
                         f'{merchants.MAX_ALIASES_PER_MERCHANT}. Remove some selected names or simplify their aliases first.')
    state = _state(conn)
    selected_ids = {target['id'], *(source['id'] for source in sources)}
    selected_aliases = [alias for alias in prepared if alias['merchant_id'] in selected_ids]
    has_broker = any(account['account_type'] == 'investment' and
                     account['bank'].lower().replace(' ', '') == 'trading212' for account in state['accounts'])
    accounts = {account['id']: account for account in state['accounts']}
    matching = []
    for original in state['transactions']:
        account = accounts.get(original['account_id'])
        if not account:
            continue
        row = {**merchants._source_row(original), 'account_type': account['account_type'],
               'bank': account['bank'], '_has_broker_account': has_broker}
        if (merchants._json(row.get('classification')).get('merchant_id') in selected_ids
                or merchants.classify(row, selected_aliases)):
            matching.append(row)
    before = {row['id']: tuple(row[field] for field in _CHANGE_FIELDS) for row in matching}
    snapshot = _copy_for_preview(conn, state)
    try:
        _move_rules(snapshot, target, sources, alias_groups)
        _categorize_preserving_source(snapshot, before)
        projected = [dict(row) for row in snapshot.execute('SELECT * FROM transactions') if row['id'] in before]
        for row in projected:
            previous_id = merchants._json(before[row['id']][-1]).get('merchant_id')
            next_id = merchants._json(row['classification']).get('merchant_id')
            if (previous_id != next_id and
                    ((previous_id is not None and previous_id not in selected_ids)
                     or (next_id is not None and next_id not in selected_ids))):
                raise merchants.AliasConflict('These aliases overlap another saved name and merging would change its matches. Review the overlapping rules before merging.')
        changed = sum(tuple(row[field] for field in _CHANGE_FIELDS) != before[row['id']] for row in projected)
    finally:
        snapshot.close()
    review_keys = {review['dedupe_key'] for review in state['overrides']}
    preserved = sum(row['dedupe_key'] in review_keys for row in matching)
    warnings = []
    if any(source['category'] != target['category'] for source in sources):
        warnings.append(f'Categories differ. The merged name uses {target["category"] or "its existing category"}; individual transaction reviews keep priority.')
    if any(alias['match_mode'] != 'exact' for aliases in alias_groups for alias in aliases):
        warnings.append('Existing prefix or contains rules are retained with their current payment contexts. No new fuzzy matching rule will be created.')
    if any(source['source'] != target['source'] for source in sources):
        warnings.append('The retained name supplies the rule metadata. Original research links, notes and saved decisions remain in merge history.')
    fingerprint = {'target_id': target['id'], 'source_ids': sorted(body.source_ids), 'state': state}
    preview_token = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, default=str).encode()).hexdigest()
    aliases = [{**group[0], 'merchant_id': target['id']} for group in alias_groups]
    result = {'target': target, 'sources': sources, 'aliases': aliases, 'alias_count': len(aliases),
              'matched_transactions': len(matching), 'changed_transactions': changed,
              'preserved_reviews': preserved, 'warnings': warnings, 'preview_token': preview_token}
    return result, before, alias_groups, state


def preview(conn, body):
    return _prepare(conn, body)[0]


def save(conn, body):
    conn.execute('BEGIN IMMEDIATE')
    try:
        result, before, alias_groups, state = _prepare(conn, body)
        if not body.preview_token or body.preview_token != result['preview_token']:
            raise merchants.AliasConflict('The names, transactions or saved decisions changed. Review a fresh merge preview before confirming.')
        target, sources = result['target'], result['sources']
        selected_ids = {target['id'], *(source['id'] for source in sources)}
        alias_ids = {alias['id'] for item in [target, *sources] for alias in item['aliases']}
        audit = {'target': target, 'sources': sources,
                 'approvals': [row for row in state['merchant_alias_approvals'] if row['alias_id'] in alias_ids],
                 'alias_sources': [row for row in state['merchant_alias_sources'] if row['alias_id'] in alias_ids],
                 'decisions': [row for row in state['merchant_suggestion_decisions'] if row['merchant_id'] in selected_ids],
                 'matched_transactions': result['matched_transactions'], 'preserved_reviews': result['preserved_reviews']}
        _move_rules(conn, target, sources, alias_groups)
        _categorize_preserving_source(conn, before)
        updated = sum(tuple(row[field] for field in _CHANGE_FIELDS) != before[row['id']]
                      for row in conn.execute('SELECT * FROM transactions') if row['id'] in before)
        analytics.recompute_all(conn, commit=False)
        merged_ids = sorted(body.source_ids)
        merge_id = conn.execute('INSERT INTO merchant_merge_history(target_id,source_ids,snapshot) VALUES(?,?,?)',
                                (target['id'], json.dumps(merged_ids), json.dumps(audit))).lastrowid
        conn.commit()
        return {'ok': True, 'target_id': target['id'], 'merged_ids': merged_ids, 'merge_id': merge_id,
                'updated_transactions': updated, 'preserved_reviews': result['preserved_reviews'],
                'message': f'Merged {len(sources) + 1} saved names into {target["canonical_name"]}. '
                           f'{len(alias_groups)} aliases retained for future imports; {updated} transactions updated. '
                           f'{result["preserved_reviews"]} individual reviews kept.'}
    except Exception:
        conn.rollback()
        raise


def _route(body, operation):
    # Import here to keep the router independent of app construction and the
    # existing test/deployment database connection override.
    from .main import get_conn
    conn = get_conn()
    try:
        if operation is preview:
            conn.execute('BEGIN')
        return operation(conn, body)
    except merchants.AliasConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        conn.close()


@router.post('/merge/preview')
def preview_merge(body: MergeRequest):
    return _route(body, preview)


@router.post('/merge')
def confirm_merge(body: MergeRequest):
    return _route(body, save)


@router.get('/merge/suggestions')
def merge_suggestions(limit: int = Query(default=20, ge=1, le=100)):
    from .main import get_conn
    from .merchant_merge_matching import suggest_merges
    conn = get_conn()
    try:
        return suggest_merges(merchants.list_merchants(conn), limit=limit)
    finally:
        conn.close()


@router.get('/{merchant_id}/merge-history')
def merge_history(merchant_id: int):
    from .main import get_conn
    conn = get_conn()
    try:
        if not conn.execute('SELECT 1 FROM merchants WHERE id=?', (merchant_id,)).fetchone():
            raise HTTPException(404, 'Merchant rule not found')
        items = []
        # Include earlier merges into a source, so provenance remains available
        # after merging the retained name into another name later.
        pending, visited = [merchant_id], set()
        while pending:
            ident = pending.pop()
            if ident in visited:
                continue
            visited.add(ident)
            for row in conn.execute('SELECT * FROM merchant_merge_history WHERE target_id=? ORDER BY id DESC', (ident,)):
                item = dict(row)
                item['source_ids'] = json.loads(item['source_ids'])
                item['snapshot'] = json.loads(item['snapshot'])
                items.append(item)
                pending.extend(item['source_ids'])
        return {'items': sorted(items, key=lambda item: item['id'], reverse=True)}
    finally:
        conn.close()

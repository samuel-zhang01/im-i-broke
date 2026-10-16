"""Offline, user-approved exact merchant rules and an inspectable review inbox."""
from collections import Counter
import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from . import merchants
from .merchant_similarity import spelling_similarity
from .workspace import category_maps, annotate_category, category_name, semantic


class QuickClassification(BaseModel):
    example_id: int = Field(ge=1)
    merchant_id: int | None = Field(default=None, ge=1)
    canonical_name: str = Field(default='', max_length=120)
    category: str = Field(default='Uncategorized', min_length=1, max_length=120)
    kind: Literal['merchant', 'person', 'self', 'financial', 'unknown'] = 'merchant'
    spending_treatment: Literal['normal', 'self_transfer', 'reimbursement'] = 'normal'
    update_category: bool = False
    confirm_own_transfer: bool = False
    preview_token: str | None = Field(default=None, max_length=64)
    additional_example_ids: list[Annotated[int, Field(ge=1)]] = Field(default_factory=list, max_length=30)


class SimilarNamesQuery(BaseModel):
    canonical_name: str = Field(default='', max_length=120)
    kind: Literal['merchant', 'person', 'self', 'financial', 'unknown'] = 'merchant'
    spending_treatment: Literal['normal', 'self_transfer', 'reimbursement'] = 'normal'
    merchant_id: int | None = Field(default=None, ge=1)
    example_id: int | None = Field(default=None, ge=1)
    patterns: list[Annotated[str, Field(max_length=256)]] = Field(default_factory=list, max_length=30)


def _rows(conn):
    _, categories = category_maps(conn)
    has_broker = bool(conn.execute("SELECT 1 FROM accounts WHERE account_type='investment' AND lower(replace(bank,' ',''))='trading212'").fetchone())
    rows = [merchants._source_row(r) for r in conn.execute("""
        SELECT t.*,a.account_type,a.bank,a.display_name AS account
        FROM transactions t JOIN accounts a ON a.id=t.account_id
        WHERE a.account_type!='investment' AND COALESCE(t.currency,'GBP')='GBP' ORDER BY t.txn_date DESC,t.id DESC
    """)]
    for row in rows:
        annotate_category(row, categories)
        row['_has_broker_account'] = has_broker
    return rows


def _label(row, personal=False):
    transfer = merchants._transfer_context(row)
    if personal and not transfer:
        raise ValueError('Personal names need a transfer description. Review this individual card payment instead.')
    # Full transfer counterparties, never the parser's truncated personal label.
    if transfer:
        label = merchants._counterparty(row.get('description'))
        return label, 'transfer'
    label = row.get('source_merchant') or row.get('description') or ''
    return label, 'card' if merchants._context_matches('card', row) else 'spending'


def _example(row):
    return {field: row.get(field) for field in ('id', 'description', 'txn_date', 'amount', 'account', 'merchant', 'category')}


def _reviews(conn):
    return {row['dedupe_key']: dict(row) for row in conn.execute('SELECT * FROM overrides')}


def _learned(rows, reviews, unknown_name='Uncategorized'):
    choices = Counter(reviews.get(row['dedupe_key'], {}).get('category') for row in rows)
    choices.pop(None, None)
    choices.pop('', None)
    choices.pop('Uncategorized', None)
    choices.pop(unknown_name, None)
    if not choices:
        return None
    category, count = choices.most_common(1)[0]
    total = sum(choices.values())
    return {'category': category, 'support': count, 'reviewed': total,
            'conflicting': total - count,
            'explanation': f'{count} of {total} saved category reviews for this exact label use {category}. Check the payment purpose before applying it to other rows.'}


def inbox(conn):
    groups, reviews = {}, _reviews(conn)
    for row in _rows(conn):
        label, context = _label(row)
        normalized = merchants.key(label)
        if not normalized:
            continue
        group = groups.setdefault((normalized, context), {'label': label, 'context': context, 'rows': []})
        group['rows'].append(row)
    items = []
    for (normalized, context), group in groups.items():
        rows = group['rows']
        outgoing = [row for row in rows if row['amount'] < 0]
        if not outgoing:
            continue
        unresolved = [row for row in outgoing if not merchants._json(row.get('classification')).get('merchant_id')
                      and not reviews.get(row['dedupe_key'], {}).get('category')
                      and row.get('spending_treatment') != 'self_transfer' and not row.get('transfer_group')]
        unknown = [row for row in unresolved if semantic(row) in (None, '', 'Uncategorized')]
        ids = sorted({merchants._json(row.get('classification')).get('merchant_id') for row in rows}
                     - {None})
        items.append({'key': context + ':' + normalized, 'label': group['label'], 'context': context,
                      'example_id': rows[0]['id'], 'n_transactions': len(rows),
                      'outgoing_total': round(-sum(row['amount'] for row in outgoing), 2),
                      'uncategorized_count': len(unknown), 'needs_identity_count': len(unresolved),
                      'categories': sorted({row.get('category') or 'Uncategorized' for row in rows}),
                      'merchant_ids': ids, 'last_date': rows[0]['txn_date'],
                      'examples': [_example(row) for row in rows[:3]],
                      'learned_suggestion': _learned(rows, reviews, category_name(conn, 'Uncategorized'))})
    items.sort(key=lambda item: (-bool(item['uncategorized_count']), -item['outgoing_total'], item['label']))
    return {'items': items, 'total_labels': len(items),
            'uncategorized_labels': sum(bool(item['uncategorized_count']) for item in items),
            'method': 'Exact source labels, separated by payment context. Outgoing totals are recorded GBP payments across imported history, before spending exclusions and refunds. Investment accounts are excluded. Suggestions use only your saved category reviews and require approval.'}


def _target(conn, body):
    if body.merchant_id is None:
        return None
    target = conn.execute('SELECT * FROM merchants WHERE id=? AND enabled=1', (body.merchant_id,)).fetchone()
    if not target:
        raise LookupError('This saved name is no longer available. Refresh and choose a current name.')
    return dict(target)


def _definition(body, target):
    return {'kind': target['kind'] if target else body.kind,
            'spending_treatment': target['spending_treatment'] if target else body.spending_treatment,
            'category': target['category'] if target and not getattr(body, 'update_category', False) else getattr(body, 'category', 'Uncategorized').strip(),
            'canonical_name': target['canonical_name'] if target else body.canonical_name.strip()}


def _proposed(example, definition, merchant_id):
    kind, treatment = definition['kind'], definition['spending_treatment']
    if kind == 'self' and treatment != 'self_transfer':
        raise ValueError('Your own account must use own-transfer treatment.')
    needs_bank_description = not merchants._transfer_context(example) and (kind == 'person' or treatment == 'self_transfer')
    if needs_bank_description:
        if not merchants._context_matches('spending_bank', example):
            if treatment == 'self_transfer':
                raise ValueError('This statement identifies a card purchase or cash transaction. Review this payment individually to mark an own-transfer exception.')
            raise ValueError('This statement identifies a card purchase or cash transaction. Review this payment individually to name a friend.')
        # Banks can omit transfer codes for payments to friends or own accounts.
        # Scope the user's rule to the full description; own-transfer exclusions
        # still require explicit confirmation when saving the preview.
        label, context = (example.get('description') or '').strip(), 'spending_bank'
    else:
        label, context = _label(example, personal=kind in {'self', 'person'})
    if context in {'card', 'transfer'}:
        context = 'spending_' + context
    if not merchants.key(label):
        raise ValueError('This row has no usable statement label. Review it individually.')
    if len(label) > 256:
        raise ValueError('This statement description is too long for a reusable exact alias. Review it individually or add a shorter scoped rule in the name directory.')
    return {**definition, 'pattern': label, 'match_mode': 'exact', 'context': context,
            'source': 'user', 'source_url': None, 'notes': '', 'merchant_id': merchant_id or -1,
            'id': -1, 'confidence': 1}


def _match_index(rows):
    index = {}
    for row in rows:
        for normalized in {merchants.key(row.get('description')), merchants.key(row.get('source_merchant')),
                           merchants._counterparty(row.get('description') or '')}:
            if normalized:
                index.setdefault(normalized, []).append(row)
    return index


def _matching(rows, proposed, match_index=None):
    possible = match_index.get(merchants.key(proposed['pattern']), []) if match_index is not None else rows
    return [row for row in possible if merchants.classify(row, [proposed])]


def _conflicts(aliases, matches, merchant_id):
    # Different exact descriptor fields and context names can still match one
    # transaction. Use the actual classifier, including broker account evidence.
    return [alias for alias in aliases if alias['merchant_id'] != merchant_id
            and alias['match_mode'] == 'exact'
            and any(merchants.classify(row, [alias]) for row in matches)]


def _similar_candidates(conn, body, rows, target, aliases, reviews, use_draft=False, match_index=None):
    definition = _definition(body, None if use_draft else target)
    example = next((row for row in rows if row['id'] == body.example_id), None) if body.example_id else None
    if body.example_id and not example:
        raise LookupError('This non-investment transaction is no longer available. Refresh the review list.')
    patterns = [definition['canonical_name'], *getattr(body, 'patterns', [])]
    primary = _proposed(example, definition, body.merchant_id) if example else None
    if primary and primary['context'] == 'spending_bank':
        # Missing bank metadata is resolved by approval of one full description.
        # Never carry that decision onto other labels via fuzzy suggestions.
        return [], {}
    if primary:
        patterns.append(primary['pattern'])
    if target:
        patterns.extend(alias['pattern'] for alias in aliases if alias['merchant_id'] == target['id'])
    anchors = {merchants.key(pattern) for pattern in patterns if merchants.key(pattern)}
    if primary and definition['kind'] in {'self', 'person'}:
        # A friendly short display name must not override the full payee
        # evidence in the transaction being assigned.
        min_parts = len(merchants.key(primary['pattern']).split())
        anchors = {anchor for anchor in anchors if len(anchor.split()) >= min_parts}
    if not any(len(anchor) >= 3 for anchor in anchors):
        return [], {}
    decisions = {(row['label_key'], row['merchant_id']) for row in conn.execute(
        "SELECT label_key,merchant_id FROM merchant_suggestion_decisions WHERE status='dismissed'")}
    groups = {}
    for row in rows:
        label, context = _label(row)
        normalized = merchants.key(label)
        if not normalized or len(label) > 256:
            continue
        if primary and normalized == merchants.key(primary['pattern']):
            continue
        context = 'spending_' + context if context in {'card', 'transfer'} else context
        if (normalized, body.merchant_id) in decisions:
            continue
        group = groups.setdefault((normalized, context), {'label': label, 'rows': []})
        group['rows'].append(row)
    items, prepared = [], {}
    match_index = _match_index(rows) if match_index is None else match_index
    personal = definition['kind'] in {'self', 'person'}
    for (normalized, context), group in groups.items():
        if not any(row['amount'] < 0 for row in group['rows']):
            continue
        if (personal or definition['spending_treatment'] == 'self_transfer') and context != 'spending_transfer':
            continue
        best = max((result for anchor in anchors if (result := spelling_similarity(anchor, normalized, personal))), default=None)
        if best is None:
            continue
        candidate = group['rows'][0]
        proposed = _proposed(candidate, definition, body.merchant_id)
        matches = _matching(rows, proposed, match_index)
        if candidate['id'] not in {row['id'] for row in matches} or _conflicts(aliases, matches, body.merchant_id):
            continue
        # A fuzzy suggestion must never take over a saved identity, even through
        # another label field or a broader existing rule. Category-only reviews
        # remain eligible and will be preserved by normal categorization.
        protected = False
        all_assigned = True
        for row in matches:
            saved_id = merchants._json(row.get('classification')).get('merchant_id')
            current = merchants.classify(row, aliases)
            current_id = current['classification']['merchant_id'] if current else None
            review = reviews.get(row['dedupe_key'], {})
            review_identity = review.get('merchant')
            if ((saved_id is not None and saved_id != body.merchant_id)
                    or (current_id is not None and current_id != body.merchant_id)
                    or (review_identity and merchants.key(review_identity) != merchants.key(definition['canonical_name']))
                    or (definition['spending_treatment'] != 'self_transfer' and (row.get('spending_treatment') == 'self_transfer' or row.get('transfer_group')))):
                protected = True
                break
            if current_id != body.merchant_id or body.merchant_id is None:
                all_assigned = False
        if protected or all_assigned:
            continue
        item = {'example_id': candidate['id'], 'label': group['label'], 'context': context,
                'n_transactions': len(matches),
                'outgoing_total': round(-sum(row['amount'] for row in matches if row['amount'] < 0), 2),
                'examples': [_example(row) for row in matches[:3]], 'score': best[0], 'reason': best[1]}
        items.append(item)
        prepared[candidate['id']] = (proposed, matches)
    items.sort(key=lambda item: (-item['score'], -item['n_transactions'], item['label'], item['context']))
    # A bounded, stable list keeps the confirmation panel useful and limits the
    # maximum batch to the same thirty aliases accepted by the save request.
    items = items[:30]
    return items, {item['example_id']: prepared[item['example_id']] for item in items}


def similar_names(conn, body):
    """Read-only suggestions while typing or selecting a saved identity."""
    rows, target = _rows(conn), _target(conn, body)
    items, _ = _similar_candidates(conn, body, rows, target, merchants._prepared(conn), _reviews(conn), use_draft=True)
    return {'items': items, 'method': 'Local spelling similarity of informative name tokens, not a probability. Review the statement examples and select only the same recipient. Approval saves each exact label in its payment context; it never creates a fuzzy matching rule.'}


def _prepare(conn, body):
    rows, target = _rows(conn), _target(conn, body)
    example = next((row for row in rows if row['id'] == body.example_id), None)
    if not example:
        raise LookupError('This non-investment transaction is no longer available. Refresh the review list.')
    definition = _definition(body, target)
    if not merchants.key(definition['canonical_name']) or ((not target or body.update_category) and not merchants.key(definition['category'])):
        raise ValueError('Choose a display name and category containing letters or numbers.')
    proposed = _proposed(example, definition, body.merchant_id)
    match_index = _match_index(rows)
    matches = _matching(rows, proposed, match_index)
    if example['id'] not in {row['id'] for row in matches}:
        raise merchants.AliasConflict('The original statement label cannot safely match this rule. Review the transaction individually.')
    aliases, reviews = merchants._prepared(conn), _reviews(conn)
    conflicts = _conflicts(aliases, matches, body.merchant_id)
    candidates, candidate_rules = _similar_candidates(conn, body, rows, target, aliases, reviews, match_index=match_index)
    selected_ids = sorted(set(body.additional_example_ids))
    if len(selected_ids) != len(body.additional_example_ids) or any(ident not in candidate_rules for ident in selected_ids):
        raise merchants.AliasConflict('A selected similar name is no longer available or safe to assign. Refresh the suggestions and select it again.')
    proposed_rules = [proposed, *(candidate_rules[ident][0] for ident in selected_ids)]
    # One transaction can appear under two exact aliases. Totals and update
    # counts describe the union, so selecting aliases cannot double-count it.
    match_ids = {row['id'] for row in matches}
    for ident in selected_ids:
        match_ids.update(row['id'] for row in candidate_rules[ident][1])
    if target and body.update_category:
        # Editing a saved category affects every alias of that identity, not
        # just the descriptor used to open this dialog. Preview the full scope.
        for row in rows:
            current = merchants.classify(row, aliases)
            if current and current['classification']['merchant_id'] == target['id']:
                match_ids.add(row['id'])
    matches = [row for row in rows if row['id'] in match_ids]
    selected = [item for item in candidates if item['example_id'] in selected_ids]
    preserved = sum(bool(reviews.get(row['dedupe_key'])) for row in matches)
    confirmation_required = proposed['context'] == 'spending_bank' and definition['spending_treatment'] == 'self_transfer'
    fingerprint = {'proposed': proposed_rules, 'selected_ids': selected_ids, 'target': target,
                   'confirm_own_transfer': body.confirm_own_transfer, 'update_category': body.update_category,
                   'matches': [(row, reviews.get(row['dedupe_key'])) for row in matches], 'conflicts': conflicts}
    token = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, default=str).encode()).hexdigest()
    preview = {'pattern': proposed['pattern'], 'context': proposed['context'], **definition,
               'updates_saved_category': bool(target and body.update_category),
               'matched_transactions': len(matches),
               'outgoing_total': round(-sum(row['amount'] for row in matches if row['amount'] < 0), 2),
               'preserved_reviews': preserved, 'examples': [_example(row) for row in matches[:5]],
               'conflict': ({'merchant_id': conflicts[0]['merchant_id'], 'canonical_name': conflicts[0]['canonical_name']} if conflicts else None),
               'own_transfer_confirmation_required': confirmation_required,
               'preview_token': token, 'learned_suggestion': _learned(matches, reviews),
               'similar_names': candidates, 'additional_aliases': selected, 'alias_count': len(proposed_rules)}
    return preview, matches, proposed_rules


def preview(conn, body):
    return _prepare(conn, body)[0]


def save(conn, body):
    from .categorize import apply_categorization
    from .analytics import recompute_all
    conn.execute('BEGIN IMMEDIATE')
    try:
        result, matches, proposed_rules = _prepare(conn, body)
        proposed = proposed_rules[0]
        if result['conflict']:
            raise merchants.AliasConflict(f'This exact label already belongs to {result["conflict"]["canonical_name"]}. Choose that saved name or edit its rule in the directory.')
        if not body.preview_token or body.preview_token != result['preview_token']:
            raise merchants.AliasConflict('The matches or saved decisions changed. Review the refreshed preview before saving.')
        if result['own_transfer_confirmation_required'] and not body.confirm_own_transfer:
            if 'confirm_own_transfer' not in body.model_fields_set:
                raise merchants.AliasConflict('Am I Broke? has been updated. Reload the page, reopen this payment, then confirm the own-account transfer before saving.')
            raise ValueError('Confirm that this exact bank statement description moves money between your own accounts before saving.')
        merchant_id = body.merchant_id
        if merchant_id is None:
            merchant_id = conn.execute("INSERT INTO merchants(canonical_name,kind,category,spending_treatment,source,notes) VALUES(?,?,?,?,'user',?)",
                (proposed['canonical_name'], proposed['kind'], proposed['category'], proposed['spending_treatment'], 'User-approved exact rule from a statement example.')).lastrowid
        elif body.update_category:
            conn.execute('UPDATE merchants SET category=?,updated_at=CURRENT_TIMESTAMP WHERE id=?', (result['category'], merchant_id))
        for proposed in proposed_rules:
            existing = next((row for row in conn.execute('SELECT * FROM merchant_aliases WHERE merchant_id=?', (merchant_id,)) if merchants.key(row['pattern']) == merchants.key(proposed['pattern']) and row['context'] == proposed['context'] and row['match_mode'] == 'exact'), None)
            alias_id = existing['id'] if existing else conn.execute("INSERT INTO merchant_aliases(merchant_id,pattern,match_mode,context) VALUES(?,?,'exact',?)", (merchant_id, proposed['pattern'], proposed['context'])).lastrowid
            conn.execute('INSERT OR IGNORE INTO merchant_alias_approvals(alias_id) VALUES(?)', (alias_id,))
            merchants.set_suggestion_decision(conn, merchant_id, proposed['pattern'], 'approved')
        fields = ('merchant', 'category', 'merchant_kind', 'spending_treatment', 'exclude_from_analysis')
        before = {row['id']: tuple(row.get(field) for field in fields) for row in matches}
        apply_categorization(conn)
        updated = sum(tuple(row[field] for field in fields) != before[row['id']] for row in conn.execute('SELECT * FROM transactions') if row['id'] in before)
        recompute_all(conn, commit=False)
        conn.commit()
        result.update(ok=True, merchant_id=merchant_id, updated_transactions=updated,
                      message=f'Saved {result["canonical_name"]}. {updated} of {len(matches)} matching transactions updated; {result["preserved_reviews"]} saved row reviews kept. {result["alias_count"]} exact label{"s" if result["alias_count"] != 1 else ""} saved for future imports in the same payment contexts.')
        return result
    except Exception:
        conn.rollback()
        raise

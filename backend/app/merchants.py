"""Persistent, explainable merchant identities; fuzzy matches are suggestions only.

No bank descriptions or personal identities are sent to a remote service. Public
merchant research is versioned in merchant_catalog.json, with source links. User
rules and per-transaction reviews always take precedence over the starter data.
"""
from __future__ import annotations

from difflib import SequenceMatcher
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import unicodedata

KINDS = {'unknown', 'merchant', 'person', 'self', 'financial'}
TREATMENTS = {'normal', 'self_transfer', 'reimbursement'}
CONTEXTS = {'any', 'transfer', 'card', 'funding', 'spending', 'spending_card', 'spending_transfer', 'spending_bank'}
MATCH_MODES = {'exact', 'prefix', 'contains'}
MAX_ALIASES_PER_MERCHANT = 100


def key(value: str | None) -> str:
    value = unicodedata.normalize('NFKD', value or '').encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]+', ' ', value.lower()).strip()


def _counterparty(description: str) -> str:
    return re.sub(r'^(?:(?:transfer|payment) (?:to|from)|to|from)\s+', '', key(description))


def _json(value):
    try:
        result = json.loads(value or '{}')
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def _transfer_context(t):
    if t.get('account_type') == 'credit':
        return False
    desc = t.get('description') or ''
    return ((t.get('type_code') or '').upper() in {'BP', 'OBP', 'DD', 'FPO', 'FPI', 'TFR', 'TRF', 'SO', 'CR'}
            or bool(re.match(r'^(?:(?:transfer|payment) (?:to|from)|to|from)\b', desc, re.I))
            or bool(re.search(r'\bself transfer\b', desc, re.I)))


def _context_matches(context, t):
    if context == 'spending_bank':
        # A user may name a friend or confirm an own transfer without a bank code.
        # Keep that exact-description decision away from card and cash rows.
        return (t.get('account_type') not in {'credit', 'investment'}
                and (t.get('type_code') or '').upper() not in {'VIS', 'DEB', 'CARD', 'POS', 'ATM', 'CASH'})
    if context in {'spending_card', 'spending_transfer'}:
        return t.get('account_type') != 'investment' and _context_matches(context.removeprefix('spending_'), t)
    if context == 'spending':
        return t.get('account_type') != 'investment'
    if context == 'transfer':
        return _transfer_context(t)
    if context == 'card':
        return t.get('account_type') == 'credit' or (t.get('type_code') or '').upper() in {'VIS', 'DEB', 'CARD'}
    if context == 'funding':
        return t.get('account_type') not in {'credit', 'investment'} and bool(t.get('_has_broker_account'))
    return True


def seed_merchants(conn):
    """Install public merchant defaults once; personal identities live only in SQLite."""
    seeds = [
        {'id': 'own-trading212', 'canonical_name': 'Trading 212 · funding', 'kind': 'financial',
         'category': 'Transfers & payments', 'spending_treatment': 'self_transfer', 'source': 'account_evidence',
         'source_url': 'https://helpcentre.trading212.com/hc/en-us/articles/360007139978-How-can-I-deposit-funds-to-my-account',
         'notes': 'Exact broker-funding descriptor on a bank account with an imported Trading 212 investment account. Credit-card purchases are excluded from this rule.',
         'aliases': [{'pattern': name, 'match_mode': 'exact', 'context': 'funding'} for name in [
             'Trading 212', 'Trading 212 London', 'Trading 212 UK G', 'Trading 212 UK LIM']]},
    ]
    catalog_path = Path(__file__).with_name('merchant_catalog.json')
    if catalog_path.exists():
        catalog = json.loads(catalog_path.read_text())
        entries = catalog if isinstance(catalog, list) else catalog.get('merchants', catalog.get('entries', []))
        for entry in entries:
            seeds.append({**entry, 'id': 'public:' + entry['id'], 'source': 'public_research',
                          'notes': entry.get('notes', '') + (f" Verified {entry['verified_at']}." if entry.get('verified_at') else '')})
    for seed in seeds:
        cur = conn.execute('INSERT OR IGNORE INTO merchants(canonical_name,kind,category,spending_treatment,source,source_url,notes,seed_key,confidence) VALUES(?,?,?,?,?,?,?,?,?)',
            (seed['canonical_name'], seed.get('kind', 'merchant'), seed.get('category'), seed.get('spending_treatment', 'normal'),
             seed.get('source', 'user_context'), seed.get('source_url'), seed.get('notes'), seed['id'], seed.get('confidence', 1.0)))
        if not cur.rowcount:
            continue
        for alias in seed.get('aliases', []):
            alias = {'pattern': alias} if isinstance(alias, str) else alias
            conn.execute('INSERT INTO merchant_aliases(merchant_id,pattern,match_mode,context) VALUES(?,?,?,?)',
                         (cur.lastrowid, alias['pattern'], alias.get('match_mode', 'exact'), alias.get('context', 'any')))


def merchant_revision(item):
    """Hash only persisted rule fields so derived match counts cannot stale edits."""
    payload = {k: v for k, v in dict(item).items() if k not in {'revision', 'n_transactions'}}
    payload['enabled'] = bool(payload.get('enabled', True))
    payload['aliases'] = sorted((dict(alias) for alias in payload.get('aliases', [])), key=lambda alias: alias['id'])
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def list_merchants(conn, include_disabled=False):
    items = [dict(r) for r in conn.execute('SELECT * FROM merchants' + ('' if include_disabled else ' WHERE enabled=1') + ' ORDER BY canonical_name,id')]
    aliases = {}
    for row in conn.execute('SELECT * FROM merchant_aliases ORDER BY id'):
        aliases.setdefault(row['merchant_id'], []).append(dict(row))
    # Parse the ledger's classification once, rather than once per saved name.
    counts = {row['merchant_id']: row['n'] for row in conn.execute("""
        SELECT json_extract(classification,'$.merchant_id') AS merchant_id,COUNT(*) AS n
        FROM transactions WHERE json_extract(classification,'$.merchant_id') IS NOT NULL
        GROUP BY merchant_id
    """)}
    for item in items:
        item['enabled'] = bool(item['enabled'])
        item['aliases'] = aliases.get(item['id'], [])
        item['revision'] = merchant_revision(item)
        item['n_transactions'] = counts.get(item['id'], 0)
    return items


def _prepared(conn):
    aliases = [dict(r) for r in conn.execute('''SELECT a.*,m.canonical_name,m.kind,m.category,m.spending_treatment,
        COALESCE(s.source,m.source) AS source,m.source_url,m.notes,m.confidence
        FROM merchant_aliases a JOIN merchants m ON m.id=a.merchant_id
        LEFT JOIN merchant_alias_sources s ON s.alias_id=a.id WHERE m.enabled=1''')]
    approved = {r['alias_id'] for r in conn.execute('SELECT alias_id FROM merchant_alias_approvals')}
    for alias in aliases:
        if alias['match_mode'] == 'exact' and alias['id'] in approved:
            alias['source'] = 'user'
            alias['confidence'] = 1.0
            alias['notes'] = 'You approved this exact alias. ' + (alias['notes'] or '')
    # Exact full identities beat a person prefix on shared-account names, even
    # when someone edits the friend's label. Within a mode, user rules win.
    return sorted(aliases, key=lambda a: (a['match_mode'] == 'exact', a['source'] == 'user', len(key(a['pattern'])), -a['id']), reverse=True)


def classify(t, aliases):
    candidates = {'description': key(t.get('description')), 'counterparty': _counterparty(t.get('description') or ''),
                  'source_merchant': key(t.get('source_merchant'))}
    for alias in aliases:
        if not _context_matches(alias['context'], t):
            continue
        pattern = key(alias['pattern'])
        if not pattern:
            continue
        for field, value in candidates.items():
            if alias['context'] == 'spending_bank' and field != 'description':
                # Rules for untyped bank payments use the complete description,
                # never a shortened parser label shared with another payment.
                continue
            # Truncated bank merchant labels aren't evidence of someone's identity.
            if alias['kind'] in {'self', 'person'} and field == 'source_merchant':
                continue
            matched = value == pattern
            if alias['match_mode'] == 'prefix':
                matched = matched or value.startswith(pattern + ' ')
            elif alias['match_mode'] == 'contains':
                matched = (' ' + pattern + ' ') in (' ' + value + ' ')
            if matched:
                return {'merchant': alias['canonical_name'], 'merchant_kind': alias['kind'],
                        'category': alias['category'], 'spending_treatment': alias['spending_treatment'],
                        'classification': {'merchant_id': alias['merchant_id'], 'alias_id': alias['id'],
                           'source': alias['source'], 'confidence': 1.0 if alias['source'] in {'user', 'user_context'} else alias['confidence'],
                           'matched_pattern': alias['pattern'], 'match_mode': alias['match_mode'],
                           'matched_field': field, 'source_url': alias['source_url'],
                           'explanation': f"{alias['match_mode'].capitalize()} match to “{alias['pattern']}” in {field.replace('_', ' ')}; {alias['context']} context. {alias['notes'] or ''}"}}
    return None


def prepared_classifier(conn):
    seed_account_evidence(conn)
    return _prepared(conn), bool(conn.execute("SELECT 1 FROM accounts WHERE account_type='investment' AND lower(replace(bank,' ',''))='trading212'").fetchone())


def _card_reference_matches(description, account_number):
    """Match masked card details only against the imported issuer account."""
    number = re.sub(r'[^0-9x*]', '', (account_number or '').lower())
    if not number:
        return False
    # Bank payees may prefix the masked number with letters, such as PB1234….
    # The issuer and imported card still have to match before this is evidence.
    for token in re.findall(r'(?<!\d)\d{4,8}[xX*•]{2,}\d{4,5}\b|\b\d{13,19}\b', description):
        # Full account numbers are available for some cards; other parsers retain
        # a masked statement identifier, which can only establish its suffix.
        prefix_suffix = re.fullmatch(r'(\d{4,8})[xX*•]{2,}(\d{4,5})', token)
        if prefix_suffix:
            prefix, suffix = prefix_suffix.groups()
            if number.isdigit() and number.startswith(prefix) and number.endswith(suffix) and len(number) >= len(prefix) + len(suffix):
                return True
            if not number.isdigit() and number.endswith(suffix) and len(re.sub(r'\D', '', number)) >= len(suffix):
                return True
        elif number.isdigit() and token == number:
            return True
    return False


def seed_account_evidence(conn):
    """Learn exact card-repayment payees from imported source evidence.

    An issuer name alone never establishes a repayment. Require a matching card
    reference, or a unique credit-side repayment plus a structured payee reference
    (or an explicit issuer direct-debit payment). Learned aliases remain local,
    editable and disableable, and survive missing later counterpart statements.
    """
    cards = [dict(r) for r in conn.execute("SELECT * FROM accounts WHERE account_type='credit'")]
    if not cards:
        return
    txns = [dict(r) for r in conn.execute("SELECT t.*,a.account_type,a.bank FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE t.txn_date IS NOT NULL")]
    credits = [t for t in txns if t['account_type'] == 'credit' and t['amount'] > 0 and re.search(
        r'payment\s+received|payment.{0,8}thank you|payment by direct debit|direct debit payment', t['description'] or '', re.I)]
    for t in txns:
        if t['account_type'] in {'credit', 'investment'} or t['amount'] >= 0 or (t['type_code'] or '').upper() not in {'BP', 'OBP', 'DD', 'FPO', 'SO', 'TFR', 'TRF'}:
            continue
        desc = t['description'] or ''
        for card in cards:
            bank = key(card['bank'])
            if bank in {'amex', 'american express'}:
                named = bool(re.match(r'^(?:AMERICAN EXPRESS|AMERICAN EXP|AMEX)\b', desc, re.I))
            else:
                named = bool(re.match(r'^' + re.escape(card['bank']) + r'\b', desc, re.I))
            if not named:
                continue
            reference = _card_reference_matches(desc, card.get('account_number'))
            has_structured_reference = bool(re.search(r'\d{4,8}[xX*•]{2,}\d{4,5}\b', desc))
            explicit_dd = (t['type_code'] or '').upper() == 'DD' and bool(re.search(r'\b(?:FIRST |REGULAR |CARD )?PAYMENT\b', desc, re.I))
            same_card = []
            for credit in credits:
                if (credit.get('currency') or 'GBP') != (t.get('currency') or 'GBP'):
                    continue
                if credit['account_id'] != card['id'] or round(credit['amount'], 2) != round(-t['amount'], 2):
                    continue
                gap = abs((date.fromisoformat(credit['txn_date']) - date.fromisoformat(t['txn_date'])).days)
                if gap <= 3:
                    same_card.append((gap, credit))
            unique_pair = bool(same_card) and sum(gap == min(g for g, _ in same_card) for gap, _ in same_card) == 1
            if not reference and not ((has_structured_reference or explicit_dd) and unique_pair):
                continue
            evidence = ('Masked payee card reference matches this imported card account.' if reference else
                        'Structured issuer payment payee is corroborated by a unique same-amount card repayment within three days.')
            seed_key = 'repayment:' + hashlib.sha256((card['id'] + ':' + key(desc)).encode()).hexdigest()[:24]
            notes = f'{evidence} Card account: {card["display_name"]}. Future exact payee matches retain repayment treatment even if the opposite statement is missing.'
            cur = conn.execute("INSERT OR IGNORE INTO merchants(canonical_name,kind,category,spending_treatment,source,notes,seed_key,confidence) VALUES(?,'financial','Transfers & payments','self_transfer','account_evidence',?,?,1)",
                (card['bank'] + ' · card repayment', notes, seed_key))
            if cur.rowcount:
                conn.execute("INSERT INTO merchant_aliases(merchant_id,pattern,match_mode,context) VALUES(?,?,'exact','transfer')", (cur.lastrowid, desc))
            break


def ledger_identity(t):
    """Structured statement evidence takes precedence over retail heuristics."""
    if (t.get('type_code') or '').upper() in {'ATM', 'CASH'}:
        return {'merchant': 'Cash withdrawal', 'merchant_kind': 'financial', 'category': 'Cash',
                'spending_treatment': 'normal', 'classification': {'source': 'statement_type',
                'confidence': 1, 'explanation': 'The statement marks this as an ATM/cash withdrawal. A shop name in the description identifies the cash-machine location, not a grocery purchase.'}}
    meta = _json(t.get('meta'))
    if key(t.get('bank')) == 'revolut' and meta.get('currency_exchange') and re.fullmatch(r'Exchanged (?:to|from) [A-Z]{3}', t.get('description') or '', re.I):
        return {'merchant': 'Revolut · currency exchange', 'merchant_kind': 'financial',
                'category': 'Transfers & payments', 'spending_treatment': 'self_transfer',
                'classification': {'source': 'statement_evidence', 'confidence': 1,
                 'movement_type': 'currency_exchange', 'exchange_fee_gbp': meta.get('exchange_fee_gbp'),
                 'foreign_wallet_coverage_unknown': True,
                 'explanation': 'This statement explicitly converts money between currency wallets. The exchange principal is excluded; any separately printed GBP exchange fee remains spending. Purchases from the foreign-currency wallet are not established by this GBP statement.'}}
    return None


def public_transaction(t):
    t = dict(t)
    t['classification'] = _json(t.get('classification'))
    t['exclude_from_analysis'] = bool(t.get('exclude_from_analysis'))
    return t


class AliasConflict(ValueError):
    """A stale or conflicting match must not silently change another rule."""


def _source_row(row):
    row = dict(row)
    row['source_merchant'] = _json(row.get('meta')).get('source_merchant') or row.get('source_merchant') or row.get('merchant')
    return row


def set_suggestion_decision(conn, merchant_id, pattern, status):
    if not key(pattern):
        raise ValueError('An alias must contain letters or numbers')
    if not conn.execute('SELECT 1 FROM merchants WHERE id=? AND enabled=1', (merchant_id,)).fetchone():
        raise LookupError('This merchant rule is no longer available. Refresh the suggestions and choose a current rule.')
    conn.execute('INSERT INTO merchant_suggestion_decisions(label_key,merchant_id,source_label,status) VALUES(?,?,?,?) ON CONFLICT(label_key,merchant_id) DO UPDATE SET source_label=excluded.source_label,status=excluded.status,updated_at=CURRENT_TIMESTAMP',
                 (key(pattern), merchant_id, pattern.strip(), status))


def dismissed_suggestions(conn):
    return [dict(r) for r in conn.execute("SELECT d.source_label AS merchant,d.merchant_id AS suggested_merchant_id,m.canonical_name AS suggested_name,d.updated_at FROM merchant_suggestion_decisions d JOIN merchants m ON m.id=d.merchant_id WHERE d.status='dismissed' AND m.enabled=1 ORDER BY d.updated_at DESC,d.label_key LIMIT 100")]


def approve_alias(conn, merchant_id, pattern, context=None, example_id=None):
    """Append one exact alias atomically; retries never replace or duplicate rules."""
    from .categorize import apply_categorization
    from .analytics import recompute_all
    if not key(pattern):
        raise ValueError('An alias must contain letters or numbers')
    conn.execute('BEGIN IMMEDIATE')
    try:
        merchant = conn.execute('SELECT * FROM merchants WHERE id=? AND enabled=1', (merchant_id,)).fetchone()
        if not merchant:
            raise LookupError('This merchant rule is no longer available. Refresh the suggestions and choose a current rule.')
        context = context or ('transfer' if merchant['kind'] in {'self', 'person'} else 'any')
        aliases = [dict(a) for a in conn.execute('SELECT a.*,m.canonical_name,m.enabled FROM merchant_aliases a JOIN merchants m ON m.id=a.merchant_id')]
        existing = next((a for a in aliases if a['merchant_id'] == merchant_id and key(a['pattern']) == key(pattern) and a['match_mode'] == 'exact' and a['context'] == context), None)
        conflicts = [a for a in aliases if a['merchant_id'] != merchant_id and a['enabled'] and key(a['pattern']) == key(pattern) and a['match_mode'] == 'exact' and (a['context'] == context or 'any' in {context, a['context']})]
        if conflicts:
            raise AliasConflict(f'This exact label already belongs to {conflicts[0]["canonical_name"]}. Edit that rule or review the transaction before assigning it elsewhere.')
        proposed = {'pattern': pattern, 'match_mode': 'exact', 'context': context,
                    'kind': merchant['kind'], 'category': merchant['category'],
                    'canonical_name': merchant['canonical_name'], 'spending_treatment': merchant['spending_treatment'],
                    'source': 'user', 'source_url': merchant['source_url'], 'notes': '',
                    'merchant_id': merchant_id, 'id': -1, 'confidence': 1}
        has_broker = bool(conn.execute("SELECT 1 FROM accounts WHERE account_type='investment' AND lower(replace(bank,' ',''))='trading212'").fetchone())
        transaction_rows = [_source_row(r) for r in conn.execute('SELECT t.*,a.account_type,a.bank FROM transactions t JOIN accounts a ON a.id=t.account_id')]
        for row in transaction_rows:
            row['_has_broker_account'] = has_broker
        candidates = [row for row in transaction_rows if classify(row, [proposed])]
        if example_id is not None:
            example = next((r for r in transaction_rows if r['id'] == example_id), None)
            if not example:
                raise LookupError('The example transaction no longer exists. Refresh the suggestions.')
            if not any(r['id'] == example_id for r in candidates):
                raise AliasConflict('The statement label or matching context has changed. Refresh the suggestion before approving it.')
        if not existing:
            cur = conn.execute("INSERT INTO merchant_aliases(merchant_id,pattern,match_mode,context) VALUES(?,?,'exact',?)", (merchant_id, pattern.strip(), context))
            alias_id = cur.lastrowid
        else:
            alias_id = existing['id']
        conn.execute('INSERT OR IGNORE INTO merchant_alias_approvals(alias_id) VALUES(?)', (alias_id,))
        set_suggestion_decision(conn, merchant_id, pattern, 'approved')
        apply_categorization(conn)
        fields = ('merchant', 'category', 'merchant_kind', 'spending_treatment', 'exclude_from_analysis')
        before = {r['id']: tuple(r.get(f) for f in fields) for r in candidates}
        updated = sum(tuple(r[f] for f in fields) != before[r['id']] for r in conn.execute('SELECT * FROM transactions') if r['id'] in before)
        reviewed_keys = {r['dedupe_key'] for r in conn.execute('SELECT * FROM overrides') if any(r[f] is not None for f in ('merchant','category','merchant_kind','spending_treatment'))}
        preserved = sum(r['dedupe_key'] in reviewed_keys for r in candidates)
        # Count direct classification changes while holding the write lock, so a
        # concurrent review cannot be mistaken for an alias-driven change.
        recompute_all(conn, commit=False)
        conn.commit()
        count = len(candidates)
        message = f'Alias saved for {merchant["canonical_name"]}. {updated} transaction{"s" if updated != 1 else ""} updated; {count} matching transaction{"s" if count != 1 else ""}.'
        if preserved:
            message += f' {preserved} saved transaction review{"s" if preserved != 1 else ""} kept.'
        if not count:
            message += ' The alias will apply to future matching imports.'
        elif not updated:
            message += ' Matching transactions already have this classification or retain your saved review.'
        return {'ok': True, 'merchant_id': merchant_id, 'canonical_name': merchant['canonical_name'],
                'alias_id': alias_id, 'pattern': pattern.strip(), 'already_exists': existing is not None,
                'matched_transactions': count, 'updated_transactions': updated,
                'preserved_reviews': preserved, 'message': message}
    except Exception:
        conn.rollback()
        raise


def suggestions(conn, limit=30):
    """Return unassigned near matches with examples; saved decisions suppress retries."""
    # Reads only: account evidence is learned by categorization, not GET requests.
    aliases = _prepared(conn)
    # Reuse the right-hand index and its normalised text across candidate labels.
    matchers = [(alias, SequenceMatcher(None, '', key(alias['pattern'])))
                for alias in aliases if alias['kind'] == 'merchant']
    decisions = {(r['label_key'], r['merchant_id']) for r in conn.execute('SELECT label_key,merchant_id FROM merchant_suggestion_decisions')}
    grouped = {}
    from .workspace import category_name
    for original in conn.execute("SELECT t.*,a.display_name AS account,a.account_type FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE merchant_kind IN ('unknown','merchant') AND (category=? OR classification IS NULL) AND COALESCE(t.currency,'GBP')='GBP' ORDER BY txn_date DESC,id DESC", (category_name(conn, 'Uncategorized'),)):
        row = _source_row(original)
        # A resolved identity is not an open fuzzy suggestion, even when someone
        # deliberately keeps Uncategorized or gives the transaction another label.
        if _json(row.get('classification')).get('merchant_id') is not None:
            continue
        name = row['source_merchant'] or row['merchant'] or ''
        normalized = key(name)
        if len(normalized) < 5:
            continue
        group = grouped.setdefault(normalized, {'name': name, 'rows': []})
        group['rows'].append(row)
    out = []
    for normalized, group in sorted(grouped.items(), key=lambda x: (-len(x[1]['rows']), x[0]))[:250]:
        name, rows = group['name'], group['rows']
        best = None
        for alias, matcher in matchers:
            if (normalized, alias['merchant_id']) in decisions:
                continue
            matcher.set_seq1(normalized)
            # These are mathematical upper bounds, so pruning preserves every
            # candidate that could meet the original similarity threshold.
            threshold = max(.78, best[0] if best else .78)
            if matcher.real_quick_ratio() < threshold or matcher.quick_ratio() < threshold:
                continue
            score = matcher.ratio()
            if .78 <= score < 1 and (best is None or score > best[0]):
                best = (score, alias)
        if best:
            score, alias = best
            processor = bool(re.match(r'^(?:sumup|sq|zettle|paypal|dojo|tst)\b', normalized))
            short_label = processor and len(normalized.split()) <= 2
            out.append({'merchant': name, 'suggested_merchant_id': alias['merchant_id'],
                        'suggested_name': alias['canonical_name'], 'category': alias['category'],
                        'similarity': round(score, 3), 'n_transactions': len(rows), 'example_id': rows[0]['id'],
                        'examples': [{k: row[k] for k in ('id','description','txn_date','amount','account')} for row in rows[:3]],
                        'match_warning': ('The processor and shortened name do not establish this business. Check the recipient before approving.' if short_label else None),
                        'explanation': 'Similar spelling only. Approving saves this exact label; it does not replace the existing merchant rule.'})
    return sorted(out, key=lambda r: (-r['n_transactions'], -r['similarity']))[:limit]

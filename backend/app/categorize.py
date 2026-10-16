"""Merchant normalization and rule-based auto-categorization.

These are deterministic, offline heuristics (the 'local-first' intelligence).
Persistent researched identities and user decisions refine the offline baseline.
"""
from __future__ import annotations
import re
import json

# Tokens/locations to strip from raw bank descriptions to recover a clean merchant.
_NOISE = [
    r"\bLONDON\b", r"\bLND\b", r"\bENG\b", r"\bGB\b", r"\bUK\b", r"\bGBR\b",
    r"\bSAN FRANCISCO\b", r"\bCA\b", r"\bCAMBRIDGE\b", r"\bON-LINE\b",
    r"\bSW\d+\w?\b", r"\b[A-Z]{1,2}\d{1,2}\s*\d?[A-Z]{0,2}\b",  # postcodes
    r"#\d+", r"\b\d{4,}\b", r"INT'L \d+", r"\bIAP\b", r"\bBKG\b",
    r"\)+", r"\(+", r"\bON \d{2}\w{3}\b", r"\d{2}:\d{2}",
]
_NOISE_RE = re.compile("|".join(_NOISE))

# Ordered (regex, category) rules. First match wins.
CATEGORY_RULES = [
    (r"netflix|spotify|disney|youtubeprem|amazon prime|apple\.com/bill|expressvpn", "Subscriptions"),
    (r"tesco|sainsbury|marks ?& ?spencer|m&s|co-?op|waitrose|aldi|lidl|morrisons|asda|ocado", "Groceries"),
    (r"pret|greggs|starbucks|caffe nero|costa|mcdonald|kfc|popeyes|burger|nando|pizza|deliveroo|uber ?eats|just ?eat|cafe|coffee|restaurant|bistro|\bespr\b|\bgail(?:s)?\b|comptoir|zizzi|ippudo", "Eating out"),
    (r"tfl|underground|lul |trainline|stagecoach|national rail|uber|bolt|waymo|clipper|transit|railway|pedibus|parking", "Transport"),
    (r"amzn|amazon|argos|currys|apple ?store|boots|wh smith|zara|h&m|uniqlo|nike|nespresso", "Shopping"),
    (r"\bcouncil\b|\bhmrc\b|\bwater\b|thames water|octopus energy|british gas|\bedf\b|e\.on|electric|utilit", "Bills & utilities"),
    (r"vodafone|\bee\b|\bo2\b|\bthree\b|giffgaff|virgin media|\bsky\b", "Phone & internet"),
    (r"payment\s*-\s*thank you|payment received|self transfer|american express.*payment|amex.*payment", "Transfers & payments"),
    (r"\brent\b|mortgage", "Housing"),
    (r"gym|puregym|fitness|academy|college", "Health & education"),
    (r"cash in|atm|cashpoint|withdrawal", "Cash"),
    (r"\binterest\b|\bcharges?\b|\bfees?\b|\boverdraft\b", "Fees & interest"),
]
_COMPILED = [(re.compile(p, re.I), c) for p, c in CATEGORY_RULES]


def normalize_merchant(description: str) -> str:
    s = description.upper()
    s = re.sub(r"^(VIS|BP|CR|DR|DD|SO|IAP|BKG)\b", "", s)
    s = _NOISE_RE.sub(" ", s)
    s = re.sub(r"[^A-Z0-9&'\- ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # keep first ~3 meaningful tokens as the merchant signature
    toks = [t for t in s.split() if len(t) > 1][:4]
    return " ".join(toks).title() or description.strip().title()


def auto_category(description: str, merchant: str, amount: float) -> str:
    text = f"{description} {merchant}"
    if amount > 0 and re.search(r"\binterest\b", text, re.I) and not re.search(r"refund|reversal", text, re.I):
        return "Income"
    for rx, cat in _COMPILED:
        if rx.search(text):
            return cat
    return "Income" if amount > 0 else "Uncategorized"


def apply_categorization(conn, *, transaction_ids=None) -> int:
    """Rebuild from preserved source, then rules, identities, and review decisions.

    A per-transaction review wins over merchant rules. Fuzzy suggestions never
    enter this path. Source descriptions, amounts, and parser metadata are never
    modified by categorization, and deletion restores the underlying merchant.
    """
    from . import merchants
    from .workspace import category_maps
    _, catalog = category_maps(conn)
    def semantic_name(value):
        return catalog.get((value or '').casefold(), {}).get('semantic_name', value)
    def display_name(value):
        return catalog.get((value or '').casefold(), {}).get('name', value)
    rules = []
    for r in conn.execute("SELECT * FROM rules ORDER BY priority, id"):
        try:
            rx = re.compile(r["pattern"] if r["is_regex"] else re.escape(r["pattern"]), re.I)
        except re.error:
            continue
        rules.append((rx, r["category"], r["id"]))
    overrides = {r["dedupe_key"]: dict(r) for r in conn.execute("SELECT * FROM overrides")}
    aliases, has_broker = merchants.prepared_classifier(conn)
    changes = []
    selected = None if transaction_ids is None else set(transaction_ids)
    for row in conn.execute("SELECT t.*, a.account_type, a.bank FROM transactions t JOIN accounts a ON a.id=t.account_id"):
        if selected is not None and row['id'] not in selected:
            continue
        t = dict(row)
        old = tuple(t.get(k) for k in ('category','category_source','merchant','source_merchant','merchant_kind','spending_treatment','exclude_from_analysis','classification'))
        meta = merchants._json(t["meta"])
        override = overrides.get(t["dedupe_key"])
        source_merchant = meta.get('source_merchant') or t.get('source_merchant')
        if source_merchant is None:
            # Legacy overridden rows may already carry a replacement label.
            source_merchant = (normalize_merchant(t['description'] or '')
                if override and override.get('merchant') is not None else t['merchant'])
        t['source_merchant'], t['_has_broker_account'] = source_merchant, has_broker
        category = meta.get("source_category")
        if not category and t["account_type"] == "investment":
            action = (t["type_code"] or "").lower()
            if "buy" in action or "sell" in action:
                category = "Investments"
            elif action in ("deposit", "withdrawal"):
                category = "Transfers & payments"
            elif action == "card credit":
                category = "Shopping"
            elif "dividend" in action or "interest" in action or "cashback" in action:
                category = "Investment income"
        category = category or auto_category(t["description"] or "", source_merchant or "", t["amount"])
        base_category = category
        source, merchant, kind, treatment, excluded = 'auto', source_merchant, 'unknown', 'normal', 0
        explanation = {'source': 'parser' if meta.get('source_category') else 'offline_heuristic',
                       'confidence': .65 if category != 'Uncategorized' else 0,
                       'explanation': 'Statement category or offline description rule; editable in transaction review.'}
        identity = merchants.classify(t, aliases)
        ledger_identity = merchants.ledger_identity(t)
        if ledger_identity and not (identity and identity['classification']['source'] == 'user'):
            identity = ledger_identity
        if identity:
            merchant, kind, treatment = identity['merchant'], identity['merchant_kind'], identity['spending_treatment']
            category = identity['category'] or category
            explanation = identity['classification']
            source = 'merchant_rule'
        for rx, rule_category, rule_id in rules:
            if rx.search(f"{t['description'] or ''} {source_merchant or ''}"):
                category, source = rule_category, 'rule'
                explanation = {**explanation, 'category_rule_id': rule_id,
                               'category_explanation': 'Saved category rule (lowest priority number wins).'}
                break
        if override:
            category = override['category'] or category
            merchant = override['merchant'] if override['merchant'] is not None else merchant
            kind = override.get('merchant_kind') or kind
            treatment = override.get('spending_treatment') or treatment
            if override.get('spending_treatment') == 'normal' and semantic_name(category) == 'Transfers & payments' and not override.get('category'):
                category = base_category if semantic_name(base_category) != 'Transfers & payments' else ('Income' if t['amount'] > 0 else 'Uncategorized')
            elif override.get('category') and semantic_name(override['category']) not in {'Transfers & payments', 'Investments'} and not override.get('spending_treatment'):
                treatment = 'normal'
            excluded = override.get('exclude_from_analysis') or 0
            source = 'user'
            explanation = {**explanation, 'reviewed': True, 'review_note': override.get('note'),
                           'review_explanation': 'Your saved transaction review takes precedence over matching rules.',
                           'reviewed_fields': [k for k, v in override.items() if k not in {'dedupe_key', 'note'} and v is not None]}
        elif t['category_source'] == 'user':
            category, source = t['category'], 'user'
        if kind == 'self' and treatment == 'normal' and not (override and (override.get('spending_treatment') == 'normal' or override.get('category'))):
            treatment = 'self_transfer'
        if treatment == 'self_transfer' and not (override and override.get('category')):
            category = 'Transfers & payments'
        category = display_name(category)
        values = (category, source, merchant, source_merchant, kind, treatment, int(excluded), json.dumps(explanation))
        if values != old:
            changes.append((*values, t['id']))
    conn.executemany("UPDATE transactions SET category=?,category_source=?,merchant=?,source_merchant=?,merchant_kind=?,spending_treatment=?,exclude_from_analysis=?,classification=? WHERE id=?", changes)
    return len(changes)

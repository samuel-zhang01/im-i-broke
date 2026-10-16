"""Local duplicate clues for saved identities, never automatic classification.

Every returned pair requires a preview and the user's merge confirmation. The
matching deliberately excludes incompatible spending treatments and keeps
personal and financial identity evidence stricter than business spelling clues.
"""
from itertools import combinations

from .merchant_similarity import spelling_similarity
from .merchants import key


def _contexts(item):
    families = set()
    for alias in item.get('aliases', []):
        context = alias.get('context', 'any')
        if context in {'card', 'spending_card'}:
            families.add('card')
        elif context in {'transfer', 'spending_transfer', 'funding', 'spending_bank'}:
            families.add('transfer')
        else:
            families.update(('card', 'transfer'))
    return families or {'card', 'transfer'}


def _exact_aliases(item):
    return {key(alias['pattern']) for alias in item.get('aliases', [])
            if alias.get('match_mode', 'exact') == 'exact' and key(alias.get('pattern'))}


def _target_rank(item):
    # Reviewed rules remain the destination even when a seeded duplicate has
    # captured more payments. Otherwise keep the most established identity.
    return (item.get('source') in {'user', 'user_context'},
            item.get('n_transactions', 0), len(item.get('aliases', [])), -item['id'])


def _match(left, right):
    if (left.get('kind') != right.get('kind')
            or left.get('spending_treatment', 'normal') != right.get('spending_treatment', 'normal')
            or not _contexts(left) & _contexts(right)):
        return None
    a, b = key(left.get('canonical_name')), key(right.get('canonical_name'))
    if not a or not b:
        return None
    financial = left.get('kind') == 'financial'
    if a == b:
        reason = 'The saved names are identical after ignoring case, accents and punctuation.'
        if financial:
            reason += ' Check the card or account references: the same provider name does not prove the same account.'
        return 'same_name', 1.0, reason
    if _exact_aliases(left) & _exact_aliases(right):
        return 'shared_alias', .99, 'Both saved names contain the same exact statement alias. Check that the labels belong to the same person or business.'
    if left.get('kind') in {'person', 'self'}:
        at, bt = a.split(), b.split()
        # Only a missing middle name qualifies: different middle names, short
        # initials, joint accounts and fuzzy surnames do not establish identity.
        short, long = sorted((at, bt), key=len)
        if (len(short) == 2 and len(long) > 2 and len(short[0]) > 1 and len(short[-1]) > 1
                and short[0] == long[0] and short[-1] == long[-1]
                and all(token.isalpha() for token in long)
                and not any(mark in str(item.get('canonical_name', '')) for item in (left, right) for mark in ('&', '/'))
                and not any(token in {'and', 'or', 'account', 'own', 'joint'} for token in long)):
            return 'middle_name', .94, 'The first and last names match exactly; one saved name adds middle names. Confirm this is the same person.'
        return None
    # Financial providers and unknown recipients need an exact identity clue;
    # similar provider spellings or reference numbers are insufficient.
    if left.get('kind') != 'merchant':
        return None
    names_a = {a} | _exact_aliases(left)
    names_b = {b} | _exact_aliases(right)
    best = None
    for name_a in sorted(names_a):
        for name_b in sorted(names_b):
            clue = spelling_similarity(name_a, name_b)
            if clue and (best is None or clue[0] > best[0]):
                best = clue
    if best:
        return 'similar_business_name', min(best[0], .93), best[1]
    return None


def suggest_merges(items, limit=20):
    """Return deterministic, ranked pairs without changing the supplied records.

    ``total`` counts all discovered pairs before the display limit. Source and
    target are recommendations only: the merge preview owns all write guards.
    """
    active = sorted((item for item in items if item.get('enabled', True)), key=lambda item: item['id'])
    results = []
    for left, right in combinations(active, 2):
        if left['id'] == right['id']:
            continue
        clue = _match(left, right)
        if not clue:
            continue
        match_type, score, reason = clue
        target, source = sorted((left, right), key=_target_rank, reverse=True)
        if left.get('category') != right.get('category'):
            reason += ' The categories differ; review the destination category before merging.'
        results.append({'target_id': target['id'], 'source_id': source['id'],
                        'target_name': target['canonical_name'], 'source_name': source['canonical_name'],
                        'match_type': match_type, 'score': score, 'reason': reason})
    results.sort(key=lambda item: (-item['score'], key(item['target_name']), item['target_id'], item['source_id']))
    return {'items': results[:max(0, limit)], 'total': len(results)}

"""Conservative local spelling clues; never a classifier or a probability model."""
from difflib import SequenceMatcher
import re

from .merchants import key

# These words identify payment rails/legal suffixes, not the recipient. They
# cannot provide the evidence that makes two different businesses look similar.
_NOISE = frozenset('payment payments purchase purchases transaction transactions transfer transfers '
                   'to from via ref reference card debit credit pos visa mastercard '
                   'paypal sumup sq zettle izettle dojo tst stripe limited ltd plc inc llc '
                   'uk gb gbr co com the and'.split())
_PROCESSORS = re.compile(r'^(?:(?:paypal|sumup|sq|zettle|izettle|dojo|tst|stripe)\s+)+')
_GENERIC = frozenset('cafe coffee restaurant restaurants shop shopping store stores supermarket '
                     'market bar hotel services service online london'.split())
_BUSINESS_SUFFIXES = frozenset('store stores superstore superstores london online'.split())


def _business_reference(token):
    # Long mixed order/terminal identifiers, never pure numbers such as a brand
    # number, or a short model/service code. Only used beside an exact brand.
    return len(token) >= 6 and sum(c.isdigit() for c in token) >= 2 and sum(c.isalpha() for c in token) >= 2


def _tokens(value, personal=False):
    if personal:
        # Initials and short surnames (B, Co, To, etc.) distinguish people.
        # Business/processor cleanup must never erase that identity evidence.
        return key(value).split()
    normalized = _PROCESSORS.sub('', key(value))
    return [token for token in normalized.split() if token not in _NOISE]


def spelling_similarity(left, right, personal=False):
    """Require compatible informative tokens, not a common processor/ref prefix.

    Every name token needs a counterpart. In particular, a shared first name
    cannot turn Cleo Example into Cleo Different, and a shared business prefix
    cannot turn Evermere into Evermere Housing. Exact aliases remain exact after
    approval; this score is only a prompt for the user's review.
    """
    a, b = _tokens(left, personal), _tokens(right, personal)
    ignored_reference = ignored_suffix = False
    if not personal:
        shared_brand = any(token.isalpha() and len(token) >= 3 and token not in _GENERIC for token in set(a) & set(b))
        if shared_brand:
            ignored_reference = any(_business_reference(token) for token in a + b)
            a, b = [token for token in a if not _business_reference(token)], [token for token in b if not _business_reference(token)]
            # Store/location words can explain an extra card descriptor, but
            # service names (Music, Housing, etc.) retain their identity value.
            if len(a) != len(b):
                ignored_suffix = any(token in _BUSINESS_SUFFIXES for token in a + b)
                a, b = [token for token in a if token not in _BUSINESS_SUFFIXES], [token for token in b if token not in _BUSINESS_SUFFIXES]
    if not a or not b or len(a) != len(b):
        return None
    # Different account/order numbers do not establish a recipient variant.
    if sorted(token for token in a if any(c.isdigit() for c in token)) != sorted(token for token in b if any(c.isdigit() for c in token)):
        return None
    remaining = list(b)
    scores = []
    informative = False
    for token in sorted(a, key=len, reverse=True):
        other = max(remaining, key=lambda value: SequenceMatcher(None, token, value).ratio())
        ratio = SequenceMatcher(None, token, other).ratio()
        # Short names/acronyms and numeric tokens require an exact match.
        threshold = 1 if min(len(token), len(other)) < 4 or any(c.isdigit() for c in token + other) else .72
        if ratio < threshold or abs(len(token) - len(other)) > 2:
            return None
        remaining.remove(other)
        scores.append(ratio)
        if token not in _GENERIC and other not in _GENERIC and len(token) >= 3 and token.isalpha() and other.isalpha():
            informative = True
    if not informative:
        return None
    # Reordering the same complete tokens is useful for exported personal names.
    score = sum(scores) / len(scores)
    if score < (.8 if personal else .78):
        return None
    reason = ('The same full name tokens appear in a different order or with small spelling changes. Check that this is the same person.' if personal else
              'The informative name tokens match closely; payment processor and company suffix words are ignored. Check that this is the same business.')
    if ignored_reference:
        reason = 'The labels share an exact business name token; long mixed letter/number reference codes were ignored. Check the examples: a reference difference does not prove the same business.'
    if ignored_suffix:
        reason += ' Generic store/location words were ignored beside an exact business name.'
    return round(score, 3), reason

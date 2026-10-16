"""Reviewable account identity consolidation from original CSV and API evidence.

This administrative operation creates an alias, never replacement transactions.
The identity proof combines complete closing holdings with independently matching
executions. A broker name, suffix or holdings match alone is insufficient.
"""
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import csv
import hashlib
import json
from pathlib import Path

from . import portfolio, source_reconciliation

RULE_VERSION = 1
EVIDENCE_KIND = 'verified_activity_reconciliation'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _time(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Identity matching requires explicit source UTC timestamps.')
    return parsed.astimezone(timezone.utc).isoformat()


def _type(action):
    action = action.upper()
    for kind in ('BUY', 'SELL', 'DIVIDEND'):
        if kind in action:
            return kind
    return action


def _decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise ValueError('Reconciliation requires finite quantities and amounts.') from None
    if not result.is_finite():
        raise ValueError('Reconciliation requires finite quantities and amounts.')
    return result


def _major(currency, amount):
    return ('GBP', amount / 100) if currency in ('GBX', 'GBp') else (currency, amount)


def _source_files(conn, source_id, root):
    """Every saved source must still be backed by identical original bytes."""
    candidates = defaultdict(list)
    for path in sorted(Path(root).rglob('*.csv')):
        candidates[hashlib.sha256(path.read_bytes()).hexdigest()].append(path)
    result = []
    for statement in conn.execute('SELECT id,meta FROM statements WHERE account_id=? ORDER BY id', (source_id,)):
        metadata = json.loads(statement['meta'] or '{}')
        digest = metadata.get('source_sha256')
        if not digest or digest not in candidates:
            raise ValueError('Every imported statement needs a byte-identical original CSV before consolidation.')
        path = candidates[digest][0]
        with path.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        result.append({'statement_id': statement['id'], 'sha256': digest,
                       'path': str(path.resolve()), 'rows': rows})
    if not result:
        raise ValueError('The imported account has no retained CSV sources.')
    return result


def _csv_events(files):
    events = {}
    for source in files:
        for line, row in enumerate(source['rows'], 2):
            # Legacy exports omit timezone; preserve them as historical evidence
            # but only explicit UTC records establish exact execution identity.
            if not row.get('Time (UTC)'):
                continue
            event_type = _type(row['Action'])
            if event_type not in ('BUY', 'SELL', 'DIVIDEND'):
                continue
            fact = {'time': _time(row['Time (UTC)']), 'type': event_type,
                    'symbol': row['Ticker'], 'quantity': str(_decimal(row['No. of shares'])),
                    'amount': str(_decimal(row['Total'])), 'currency': row['Currency (Total)'],
                    'provider_id': row['ID']}
            identity = _digest(fact)
            event = events.setdefault(identity, {**fact, 'sources': []})
            event['sources'].append({'sha256': source['sha256'], 'line': line})
    return list(events.values())


def _holdings(conn, account_id):
    expected = {}
    for position in portfolio.holding_positions(conn, account_ids=[account_id]).values():
        amount = _decimal(position['net_shares'])
        if abs(amount) <= Decimal('.000001'):
            continue
        currency, _ = _major(position.get('execution_price_currency') or position.get('native_currency'), Decimal(1))
        if not currency:
            raise ValueError('Imported holdings have no supported listing currency.')
        key = (position['ticker'], currency)
        if key in expected or amount < 0:
            raise ValueError('Imported holdings have an ambiguous symbol or negative quantity.')
        expected[key] = amount
    return expected


def _position_match(expected, account):
    positions, activity = account.get('positions', {}), account.get('activities', {})
    if any(section.get('status') != 'ok' or section.get('truncated') or section.get('stale')
           for section in (positions, activity)):
        return False
    actual = {}
    for position in positions.get('data', []):
        if position.get('is_cash'):
            continue
        quantity = _decimal(position.get('quantity'))
        if abs(quantity) <= Decimal('.000001'):
            continue
        symbol = position.get('raw_symbol')
        currency, _ = _major(position.get('listing_currency'), Decimal(1))
        key = (symbol, currency)
        if not symbol or not currency or key in actual:
            return False
        actual[key] = quantity
    return expected.keys() == actual.keys() and all(abs(value - actual[key]) <= Decimal('.000001') for key, value in expected.items())


def _activity_evidence(events, account):
    lookup = defaultdict(list)
    for event in events:
        lookup[(event['time'], event['type'], event['symbol'])].append(event)
    first, last = min(event['time'] for event in events), max(event['time'] for event in events)
    matches = []
    seen = set()
    for activity in account['activities']['data']:
        when = _time(activity['date'])
        if activity['type'] not in ('BUY', 'SELL', 'DIVIDEND') or not first <= when <= last:
            continue
        symbol = (activity.get('symbol') or '').removesuffix('.L').removesuffix('.DE')
        candidates = lookup[(when, activity['type'], symbol)]
        if len(candidates) != 1:
            raise ValueError('API activity within the CSV overlap is missing or ambiguous.')
        event = candidates[0]
        identity = (when, activity['type'], symbol)
        if identity in seen:
            raise ValueError('Duplicate API activity makes execution identity ambiguous.')
        seen.add(identity)
        if abs(abs(_decimal(activity['quantity'])) - abs(_decimal(event['quantity']))) > Decimal('.00000001'):
            raise ValueError('An overlapping activity quantity conflicts with the original CSV.')
        match = {'api_activity_id': activity['id'], **event}
        if activity['type'] in ('BUY', 'SELL'):
            currency, amount = _major(activity['currency'], _decimal(activity['amount']))
            source_currency, source_amount = _major(event['currency'], _decimal(event['amount']))
            source_amount = -abs(source_amount) if activity['type'] == 'BUY' else abs(source_amount)
            if currency != source_currency or abs(amount - source_amount) > Decimal('.01'):
                raise ValueError('An overlapping execution cash amount conflicts with the original CSV.')
            match['cash_verified'] = True
        matches.append(match)
    trades = [row for row in matches if row.get('cash_verified')]
    if (len(matches) < 10 or len(trades) < 3 or len({row['time'][:10] for row in trades}) < 2
            or len({row['symbol'] for row in trades}) < 2):
        raise ValueError('Insufficient independent matching executions to establish account identity.')
    return matches


def _verify_saved_rows(conn, source_id, matches, files):
    """Ensure matched original events actually belong to this saved ledger."""
    originals = {(source['sha256'], line): row for source in files
                 for line, row in enumerate(source['rows'], 2)}
    saved = defaultdict(list)
    for transaction in conn.execute('SELECT id,meta FROM transactions WHERE account_id=?', (source_id,)):
        metadata = json.loads(transaction['meta'] or '{}')
        for observation in metadata.get('source_observations', []):
            key = (observation.get('source_sha256'), observation.get('source_line'))
            if key in originals and observation.get('source_row') == originals[key]:
                saved[key].append(transaction['id'])
    for match in matches:
        ids = {identifier for source in match['sources']
               for identifier in saved[(source['sha256'], source['line'])]}
        if len(ids) != 1:
            raise ValueError('A matched original event lacks a unique saved transaction with identical source-row evidence.')
        match['saved_transaction_id'] = next(iter(ids))


def build_manifest(conn, *, source_id, data_root):
    """Dry-run proof; reads source bytes and database state without mutations."""
    source = conn.execute('SELECT * FROM accounts WHERE id=?', (source_id,)).fetchone()
    if not source or source['account_type'] != 'investment' or 'trading212' not in source['bank'].lower().replace(' ', ''):
        raise ValueError('Choose an imported Trading 212 investment account.')
    files = _source_files(conn, source_id, data_root)
    events = _csv_events(files)
    if not events:
        raise ValueError('No explicit UTC source events are available for identity verification.')
    row = conn.execute('SELECT payload FROM snaptrade_cache WHERE id=1').fetchone()
    if not row:
        raise ValueError('A saved independent connected portfolio is required.')
    cached = json.loads(row['payload'])
    expected = _holdings(conn, source_id)
    if len(expected) < 5:
        raise ValueError('Insufficient independent holdings to establish account identity.')
    candidates = [account for account in cached.get('accounts', [])
                  if not account.get('is_paper') and 'trading212' in account.get('institution', '').lower().replace(' ', '')
                  and _position_match(expected, account)]
    if len(candidates) != 1:
        raise ValueError('The complete holdings do not identify exactly one connected account.')
    account = candidates[0]
    # Another imported ledger with the same closing holdings makes source
    # identity ambiguous even when the broker only exposes one live account.
    for other in conn.execute("SELECT id FROM accounts WHERE account_type='investment' AND id!=?", (source_id,)):
        if _holdings(conn, other['id']) == expected:
            raise ValueError('Another imported account has the same complete holdings; identity is ambiguous.')
    last = max(event['time'] for event in events)
    observed = _time(account['holdings_synced_at'])
    if not 0 <= (datetime.fromisoformat(observed) - datetime.fromisoformat(last)).total_seconds() <= 7 * 86400:
        raise ValueError('The broker holdings must be observed within seven days after the CSV endpoint.')
    if any(_time(event['date']) > last and event['type'] in ('BUY', 'SELL', 'TRANSFER', 'REI', 'SPLIT')
           for event in account['activities']['data']):
        raise ValueError('Later share-changing activity prevents a direct endpoint reconciliation.')
    matches = _activity_evidence(events, account)
    _verify_saved_rows(conn, source_id, matches, files)
    manifest = {'rule_version': RULE_VERSION, 'evidence_kind': EVIDENCE_KIND,
                'source_id': 'statement:' + source_id, 'canonical_id': 'connected:' + account['id'],
                'source_files': [{key: value for key, value in source.items() if key != 'rows'} for source in files],
                'portfolio_payload_sha256': hashlib.sha256(row['payload'].encode()).hexdigest(),
                'holdings_observed_at': observed, 'holdings_count': len(expected),
                'holdings': [{'symbol': key[0], 'currency': key[1], 'quantity': str(value)}
                             for key, value in sorted(expected.items())],
                'matching_events': matches, 'matching_event_count': len(matches),
                'cash_verified_execution_count': sum(bool(item.get('cash_verified')) for item in matches),
                'identity_is_unique': True,
                'limitations': ['CSV exports contain no printed account number; identity is established by independent execution and holdings reconciliation.',
                                'Dividend cash amounts and historical cash completeness are not established by this identity link.']}
    manifest['evidence_digest'] = _digest(manifest)
    return manifest


def apply_manifest(conn, manifest, *, data_root):
    """Revalidate the reviewed proof and atomically retain an auditable alias."""
    if conn.in_transaction:
        raise ValueError('Consolidation requires its own database transaction.')
    # Schema creation precedes BEGIN because the legacy schema helper uses an
    # executescript. The transaction below covers fresh verification and writes.
    source_reconciliation.ensure_schema(conn)
    conn.execute('BEGIN IMMEDIATE')
    try:
        source_id = manifest['source_id'].removeprefix('statement:')
        actual = build_manifest(conn, source_id=source_id, data_root=data_root)
        if actual != manifest:
            raise ValueError('Source evidence changed after review; generate and review a fresh manifest.')
        existing = next((row for row in source_reconciliation.list_links(conn)
                         if row['source_id'] == actual['source_id']), None)
        if existing:
            if existing['canonical_id'] != actual['canonical_id']:
                raise ValueError('The source already links to a different canonical account.')
            conn.rollback()
            return False
        # Avoid the helper's executescript inside this transaction. All identity
        # and kind checks have been performed above against actual stored sources.
        mapping = {row['source_id']: row['canonical_id'] for row in source_reconciliation.list_links(conn)}
        mapping[actual['source_id']] = actual['canonical_id']
        for identifier in mapping:
            source_reconciliation.resolve(identifier, mapping)
        now = datetime.now(timezone.utc).isoformat()
        values = (actual['source_id'], actual['canonical_id'], EVIDENCE_KIND, actual['evidence_digest'], now, now)
        conn.execute('''INSERT INTO wealth_source_links VALUES(?,?,?,?,?,?,1)
            ON CONFLICT(source_id) DO UPDATE SET canonical_id=excluded.canonical_id,
            evidence_kind=excluded.evidence_kind,evidence_digest=excluded.evidence_digest,
            updated_at=excluded.updated_at,active=1''', values)
        conn.execute('''INSERT INTO wealth_source_link_events
            (source_id,canonical_id,action,evidence_kind,evidence_digest,recorded_at) VALUES(?,?,'link',?,?,?)''',
            (actual['source_id'], actual['canonical_id'], EVIDENCE_KIND, actual['evidence_digest'], now))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise

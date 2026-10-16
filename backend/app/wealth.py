"""Observed wealth across statements and connected accounts, with dated FX.

No market/provider calls on reads. Unknown opening positions are never zeroed,
and a new account entering coverage is not presented as investment performance.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import json
import hashlib
import re

from . import snaptrade_store
from . import lunchflow_store
from . import source_reconciliation
from .balance_resolution import resolve_credit_balance
from .bank_balance_rules import available_credit_rule
from .snaptrade_models import currency, number
from .workspace import reporting_today

RANGES = {'1w': 7, 'ytd': None, '1m': 30, '3m': 90, '6m': 180, '1y': 365, 'all': None}


def day(value):
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except (ValueError, TypeError):
        return None


def metadata(value):
    try:
        result = json.loads(value or '{}')
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def institution(value):
    key = re.sub(r'[^a-z0-9]', '', (value or '').lower())
    if 'moomoo' in key or 'futu' in key:
        return 'moomoo'
    if 'trading212' in key:
        return 'trading212'
    return key


def observation(date_value, value, code, source, *, estimated=False, reconciled=True,
                source_time_known=None, retrieved_at=None):
    result = {'date': day(date_value), 'value': number(value), 'currency': currency(code),
              'source': source, 'estimated': estimated, 'reconciled': reconciled}
    if source == 'recorded':
        # Retain the balance clock until same-day observations are resolved.
        # A positions refresh can be newer than the broker's account total.
        result.update(observed_at=date_value, source_time_known=bool(source_time_known),
                      retrieved_at=retrieved_at or date_value)
        try:
            observed = datetime.fromisoformat(str(date_value).replace('Z', '+00:00'))
            if observed.tzinfo is not None:
                result['date'] = observed.astimezone(timezone.utc).date().isoformat()
        except ValueError:
            pass
    return result


def observation_time(point, *, receipt=False):
    try:
        raw = point.get('retrieved_at') if receipt else None
        value = datetime.fromisoformat(str(raw or point.get('observed_at') or point['date']).replace('Z', '+00:00'))
        return value.replace(tzinfo=timezone.utc).timestamp() if value.tzinfo is None else value.timestamp()
    except (ValueError, TypeError):
        return float('-inf')


def older_recorded(candidate, previous):
    # Broker clocks are comparable only when both were independently saved.
    # A legacy receipt at 16:00 must not outrank a fresh receipt at 17:00 merely
    # because the latter finally supplies the actual 09:00 balance clock.
    receipt = not (candidate.get('source_time_known') and previous.get('source_time_known'))
    candidate_time = observation_time(candidate, receipt=receipt)
    previous_time = observation_time(previous, receipt=receipt)
    if candidate_time != previous_time:
        return candidate_time < previous_time
    if receipt and candidate.get('source_time_known') != previous.get('source_time_known'):
        return not candidate.get('source_time_known')
    return observation_time(candidate, receipt=True) < observation_time(previous, receipt=True)


def current_observation(account):
    points = account['observations']
    if account['source'] == 'snaptrade' or account.get('recorded_only_from'):
        # Provider backfill can be dated later than a broker's reported total.
        # Its estimate must never become the authoritative current balance.
        recorded = [point for point in points if point['source'] == 'recorded']
        if recorded:
            latest = None
            for point in sorted(recorded, key=lambda item: observation_time(item, receipt=True)):
                if latest is None or not older_recorded(point, latest):
                    latest = point
            return latest
    return points[-1] if points else None


def statement_running_balances(conn, statement, code, sign):
    """Use dated printed balances only when the whole statement chain agrees."""
    if statement['reconciled'] != 1 or code != 'GBP':
        return []
    opening = number(statement['opening_balance'])
    closing = number(statement['closing_balance'])
    if opening is None or closing is None:
        return []
    rows = conn.execute('SELECT txn_date,amount,balance FROM transactions WHERE statement_id=? ORDER BY id',
                        (statement['id'],)).fetchall()
    if not rows:
        return []
    running, previous, dated = opening, None, {}
    for row in rows:
        when, amount, printed = day(row['txn_date']), number(row['amount']), number(row['balance'])
        if when is None or amount is None or (previous is not None and when < previous):
            return []
        running += amount
        if printed is not None:
            if abs(running - printed) > .015:
                return []
            dated[when] = printed
        previous = when
    if abs(running - closing) > .015:
        return []
    return [observation(when, sign * value, code, 'statement') for when, value in dated.items()]


def sources(conn, *, path=None, today=None, provider_today=None):
    """Projection contains suffixes/names only, never full account numbers."""
    from .wealth_investments import recorded_cutover
    from .account_settings import effective_settings
    from .workspace import exists

    # User-entered dates follow the workspace calendar. Provider observation
    # dates retain their UTC evidence clock, including a fresh receipt after
    # UTC midnight when the workspace is still on the previous calendar day.
    provider_today = provider_today or today or datetime.now(timezone.utc).date()
    today = today or reporting_today(conn)
    cached = snaptrade_store.load_portfolio(path=path)
    connected = (cached or {}).get('accounts', [])
    bank_balances = lunchflow_store.latest_balances(conn, resolve=False)
    bank_history = defaultdict(list)
    for point in reversed(lunchflow_store.balance_history(conn, limit=100000)):
        bank_history[point['account_id']].append(point)
    credit_valuations = defaultdict(list)
    for point in lunchflow_store.credit_valuation_history(conn):
        credit_valuations[point['account_id']].append(point)
    accounts = []
    configured_settings = ({'statement:' + row['account_id']: dict(row) for row in conn.execute(
        'SELECT account_id,include_in_reports,archived,ownership_share FROM account_settings')}
        if exists(conn, 'account_settings') else {})
    for raw_account in conn.execute('SELECT * FROM accounts ORDER BY bank,id'):
        row = {'currency': 'GBP', **dict(raw_account)}
        investment = row['account_type'] == 'investment'
        settings = effective_settings(conn, row['id'])
        account = {'id': 'statement:' + row['id'], 'name': row['display_name'] or row['bank'],
                   'institution': row['bank'], 'kind': 'investment' if investment else ('credit' if row['account_type'] == 'credit' else 'cash'),
                   'source': 'statement', 'currency': None if investment else row['currency'] or 'GBP',
                   'default_included': settings['include_in_reports'] and not settings['archived'], 'is_paper': False,
                   'ownership_share': settings['ownership_share'],
                   'exclusion_reason': 'Excluded in account settings.' if not settings['include_in_reports'] or settings['archived'] else None,
                   'drilldown': {'page': 'investments' if investment else 'banking', 'account_id': row['id']},
                   'observations': [], 'forced_stale': False}
        if row['account_type'] == 'credit':
            account['debt_credit_observations'] = []
            for saved in credit_valuations[row['id']]:
                point = observation(saved['fetched_at'], saved['effective_amount'], saved['currency'],
                    'recorded_credit', estimated=bool(saved['estimated']))
                point.update(observed_at=saved['fetched_at'],
                    valuation_basis=('available_credit' if saved['balance_type'] == 'available_credit'
                                     else 'recorded_balance'),
                    evidence_kind=saved['evidence_kind'],
                    balance_observation_id=saved['balance_observation_id'],
                    rule_confirmed_at=saved['rule_confirmed_at'],
                    attested_at=saved['attested_at'])
                account['debt_credit_observations'].append(point)
            if account['debt_credit_observations']:
                dates = {point['date'] for point in account['debt_credit_observations']}
                account['credit_valuation_coverage'] = {
                    'first_date':min(dates), 'last_date':max(dates), 'days':len(dates),
                    'estimated_days':len({point['date'] for point in account['debt_credit_observations']
                        if point['estimated']})}
        for statement in conn.execute('SELECT id,period_start,period_end,opening_balance,closing_balance,reconciled,meta FROM statements WHERE account_id=? ORDER BY period_end,id', (row['id'],)):
            meta = metadata(statement['meta'])
            reconciled = statement['reconciled'] == 1
            if investment:
                # Broker-reported NAV includes cash; reconstructed cost is not NAV.
                if meta.get('parser') == 'moomoo':
                    account['observations'].append(observation(meta.get('snapshot_date') or statement['period_end'],
                        meta.get('net_asset_value'), meta.get('base_currency'), 'statement',
                        reconciled=meta.get('native_reconciled') is True))
                    if meta.get('previous_snapshot_date') and number(meta.get('opening_net_asset_value')) is not None:
                        account['observations'].append(observation(meta['previous_snapshot_date'],
                            meta['opening_net_asset_value'], meta.get('base_currency'), 'statement',
                            reconciled=meta.get('native_reconciled') is True))
            else:
                sign = -1 if row['account_type'] == 'credit' else 1
                closing = number(statement['closing_balance'])
                if closing is not None:
                    account['observations'].append(observation(statement['period_end'], sign * closing, row['currency'] or 'GBP', 'statement', reconciled=reconciled))
                opening = number(statement['opening_balance'])
                start = day(statement['period_start'])
                if opening is not None and start and reconciled:
                    prior = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
                    account['observations'].append(observation(prior, sign * opening, row['currency'] or 'GBP', 'statement', reconciled=True))
                if exists(conn, 'transactions'):
                    account['observations'].extend(statement_running_balances(
                        conn, statement, row['currency'] or 'GBP', sign))
        if settings['origin'] == 'manual' and exists(conn, 'manual_balance_observations'):
            for point in conn.execute('SELECT amount,currency,as_of FROM manual_balance_observations WHERE account_id=? ORDER BY as_of,id', (row['id'],)):
                account['observations'].append(observation(point['as_of'], point['amount'], point['currency'], 'manual', reconciled=None))
        # A bank feed extends the SAME account's statement history. It never
        # creates a second wealth source for the same money.
        if not investment and row['id'] in bank_balances:
            current_bank = bank_balances[row['id']]
            if row['account_type'] == 'credit':
                from .bank_balance_rules import rule_history
                account['credit_rule_events'] = rule_history(conn, current_bank['connection_id'])
            if (row['account_type'] == 'credit'
                    and available_credit_rule(conn, row['id'], current_bank['currency'])):
                account['provider_balance_type'] = 'available_credit'
            account.update(source='lunchflow', currency=current_bank['currency'],
                           forced_stale=bool(current_bank.get('stale') or current_bank.get('error')),
                           fetched_at=current_bank['fetched_at'], bank_as_of=None)
            for point in bank_history[row['id']]:
                observed = observation(point['fetched_at'], point['amount'],
                    point['currency'], 'lunchflow', reconciled=True)
                observed['fetched_at'] = point['fetched_at']
                account['observations'].append(observed)
        accounts.append(account)

    recorded, estimates, archived = defaultdict(list), defaultdict(list), {}
    observed_rows = snaptrade_store.list_account_observations(path=path)
    estimate_rows = snaptrade_store.list_account_estimates(path=path)
    for item in estimate_rows:
        estimates[item['account_id']].append(observation(item['date'], item['total_value'], item['currency'], 'snaptrade_estimate', estimated=True))
        archived[item['account_id']] = item
    for item in observed_rows:
        recorded[item['account_id']].append(observation(item.get('observed_at') or item['retrieved_at'],
            item.get('total_value'), item.get('currency'), 'recorded', estimated=False,
            source_time_known=item.get('source_time_known', False), retrieved_at=item['retrieved_at']))
        archived[item['account_id']] = item
    current_ids = {a['id'] for a in connected}
    for id, row in archived.items():
        if id in current_ids:
            continue
        accounts.append({'id': 'connected:' + id, 'name': row['name'] + ' · Archived',
            'institution': row['institution'], 'kind': 'investment', 'source': 'snaptrade',
            'currency': row.get('currency'), 'default_included': False, 'archived': True,
            'is_paper': row.get('is_paper') is True,
            'drilldown': {'page': 'connected-portfolio', 'account_id': id},
            'exclusion_reason': 'No longer returned by SnapTrade. Saved historical observations are retained, but its current value is unknown.',
            'observations': estimates[id] + recorded[id], 'forced_stale': True})
    disabled = {c['id'] for c in (cached or {}).get('connections', {}).get('data', []) if c.get('disabled')}
    for row in connected:
        suffix = (' · ' + row['number_suffix']) if row.get('number_suffix') else ''
        paper = row.get('is_paper') is True
        disconnected = row.get('connection_disconnected') is True
        account = {'id': 'connected:' + row['id'], 'name': row['name'] + suffix,
                   'institution': row['institution'], 'kind': 'investment', 'source': 'snaptrade',
                   'currency': row.get('currency'), 'default_included': not paper and not disconnected,
                   'is_paper': paper,
                   'drilldown': {'page': 'connected-portfolio', 'account_id': row['id']},
                   'exclusion_reason': ('Disconnected provider connection: saved financial history is retained and excluded from default net worth.'
                                        if disconnected else 'Paper account: simulated money, excluded from real net worth.' if paper else None),
                   'observations': list(estimates[row['id']]), 'forced_stale': bool(row.get('connection_stale', (cached or {}).get('cache', {}).get('stale')) or row.get('connection_id') in disabled)}
        for point in row.get('history', {}).get('data', []):
            account['observations'].append(observation(point.get('date'), point.get('total_value'), point.get('currency'), 'snaptrade_estimate', estimated=True))
        account['observations'].extend(recorded[row['id']])
        # Keep the current cached total visible even before a migration has
        # observations, or when a previously-known total becomes unavailable.
        balance_as_of = row.get('total_value_as_of')
        account['observations'].append(observation(balance_as_of or row.get('retrieved_at') or (cached or {}).get('retrieved_at'),
            row.get('total_value'), row.get('currency'), 'recorded', source_time_known=bool(balance_as_of),
            retrieved_at=row.get('retrieved_at') or (cached or {}).get('retrieved_at')))
        accounts.append(account)
    for account in accounts:
        if account['kind'] == 'credit':
            # Keep dated debt evidence separate: same-day available-credit
            # observations otherwise replace statement anchors during dedup.
            account['debt_statement_observations'] = sorted(
                (point for point in account['observations'] if point['source'] == 'statement'
                 and point['reconciled'] and point['value'] is not None and point['date']
                 and point['date'] <= today.isoformat()), key=lambda point: point['date'])
        # Retain the first receipt before same-day aggregation discards older
        # snapshots. A later receipt of the same dated balance cannot move the
        # account's live-history boundary forward.
        recorded_from = recorded_cutover(account)
        if recorded_from:
            account['recorded_only_from'] = recorded_from
        points = {}
        # Resolve mixed legacy/new snapshots in receipt order, independently of
        # SQL ordering by their different kinds of effective source timestamp.
        observations = [item for item in account['observations'] if item['source'] != 'recorded']
        observations.extend(sorted((item for item in account['observations'] if item['source'] == 'recorded'),
                                   key=lambda item: observation_time(item, receipt=True)))
        for item in observations:
            cutoff = provider_today if item['source'] in {'lunchflow', 'recorded', 'snaptrade'} else today
            if item['date'] and item['date'] <= cutoff.isoformat():
                old = points.get(item['date'])
                if (old and old['source'] == item['source'] == 'recorded'
                        and older_recorded(item, old)):
                    continue  # A later retrieval must not replace a newer balance with an older one.
                if (account['source'] == 'statement' and old and old['source'] == item['source'] == 'statement'
                        and old['value'] != item['value']):
                    # Conflicting statement anchors cannot establish a balance.
                    item = {**item, 'value': None, 'reconciled': False}
                # Later recorded observations override provider estimates.
                points[item['date']] = item
        account['observations'] = [points[d] for d in sorted(points)]
        if account['source'] == 'statement' and account['kind'] == 'investment' and not any(
                point['value'] is not None and point['currency'] is not None for point in account['observations']):
            account.update(default_included=False, history_only=True,
                           exclusion_reason='Statement activity only; no supported account balance. History remains in archive.')
    source_reconciliation.apply_links(accounts, source_reconciliation.list_links(conn))
    source_reconciliation.apply_archive_preferences(accounts, source_reconciliation.list_archive_preferences(conn))
    # Settings belong to the verified account identity. Resolve the policy
    # after joining sources, before projecting ownership onto its values.
    groups = defaultdict(list)
    for account in accounts:
        groups[account.get('canonical_id', account['id'])].append(account)
    for members in groups.values():
        settings = [configured_settings[member['id']] for member in members if member['id'] in configured_settings]
        if not settings:
            continue
        shares = {setting['ownership_share'] for setting in settings}
        excluded = any(not setting['include_in_reports'] or setting['archived'] for setting in settings)
        for account in members:
            if len(shares) == 1:
                account['ownership_share'] = next(iter(shares))
            else:
                account['ownership_share'] = None
                account['settings_warning'] = 'Linked account sources have conflicting ownership shares. Set the same ownership share on their account settings before including a value.'
            if excluded:
                account.update(default_included=False, exclusion_reason='Excluded in account settings.' if len(members) == 1
                               else 'Excluded in account settings for a linked source of this account.')
    return accounts, cached


def _project_ownership(account):
    """Apply ownership after full-balance reconciliation and reconstruction."""
    share = account.get('ownership_share', 1)
    if share == 1:
        return
    def scale(value):
        return value * share if value is not None and share is not None else None
    for field in ('observations', 'history_observations', 'debt_statement_observations',
                  'debt_credit_observations'):
        if field in account:
            account[field] = [point | {'full_value': point.get('full_value', point['value']),
                                      'value': scale(point.get('full_value', point['value']))}
                              for point in account[field]]
    for field in ('native_value', 'value', 'reported_native_value', 'reported_value'):
        if field in account:
            account['full_' + field] = account[field]
            account[field] = scale(account[field])
    if account.get('valuation'):
        valuation = account['valuation']
        account['valuation'] = {**valuation, 'full_effective_value': valuation['effective_value'],
                                'effective_value': scale(valuation['effective_value']), 'ownership_share': share}
        if share is None:
            account['valuation'].update(current_unknown=True, basis='unknown', explanation=account['settings_warning'])


class Rates:
    def __init__(self, conn):
        self.values = defaultdict(dict)
        for row in conn.execute('SELECT currency,date,rate_to_gbp,observed_date FROM reference_fx ORDER BY date'):
            rate = number(row['rate_to_gbp'])
            observed = day(row['observed_date'])
            requested = day(row['date'])
            code = currency(row['currency'])
            if rate is not None and rate > 0 and observed and requested and code and observed <= requested:
                self.values[code][observed] = rate
        self.dates = {code: sorted(points) for code, points in self.values.items()}

    def rate(self, code, when):
        if code == 'GBP':
            return 1.0
        dates = self.dates.get(code, [])
        index = bisect_right(dates, when) - 1
        if index < 0:
            return None
        observed = dates[index]
        if (date.fromisoformat(when) - date.fromisoformat(observed)).days > 7:
            return None
        return self.values[code][observed]

    def convert(self, value, original, target, when):
        if value is None or original is None:
            return None
        if original == target:
            return value
        a, b = self.rate(original, when), self.rate(target, when)
        return value * a / b if a is not None and b is not None else None


def bundle(conn, *, path=None, currency_code='GBP', range_name='all', account_ids=None, today=None):
    provider_today = today or datetime.now(timezone.utc).date()
    today = today or reporting_today(conn)
    from .currency_reporting import currency_code as validate_currency, currency_precision
    validate_currency(currency_code)
    precision = currency_precision(currency_code)
    if range_name not in RANGES:
        raise ValueError('Choose a supported currency and date range.')
    accounts, cached = sources(conn, path=path, today=today, provider_today=provider_today)
    ids = {a['id'] for a in accounts}
    if account_ids is None:
        requested = [a['id'] for a in accounts if a['default_included']]
    else:
        requested = [] if account_ids == ['__none__'] else list(dict.fromkeys(account_ids))
        from .account_selection import MAX_SELECTED_ACCOUNTS
        if len(account_ids) > MAX_SELECTED_ACCOUNTS or any(value not in ids for value in requested):
            raise ValueError('Choose valid accounts from the account selector.')
    # A verified alias always resolves to its canonical account, even when a
    # caller explicitly selects both. Sources remain inspectable but cannot be
    # added twice in the same aggregate.
    canonical = {a['id']: a.get('canonical_id', a['id']) for a in accounts}
    requested = list(dict.fromkeys(canonical[identifier] for identifier in requested))
    selected = [a for a in accounts if a['id'] in requested]
    rates = Rates(conn)
    cutoff = date(today.year, 1, 1) if range_name == 'ytd' else today - timedelta(days=RANGES[range_name]) if RANGES[range_name] else None
    from .wealth_investments import add_history, history_continuity, observation_coverage
    reconstruction_issues = add_history(conn, accounts, cached, rates, today=today, selected_ids=requested)
    # A reporting target need not be held in an account. Missing dated FX
    # remains unknown, with the normal partial-total explanation and controls.
    currencies = sorted({'GBP', currency_code} | {p['currency'] for a in accounts for p in a['observations'] if p['currency']})
    for account in accounts:
        latest = current_observation(account)
        if latest and latest['source'] == 'manual':
            account['balance_source'] = 'manual'
        account['native_value'] = latest['value'] if latest else None
        account['currency'] = latest['currency'] if latest else account['currency']
        account['as_of'] = (latest.get('observed_at') or latest['date']) if latest else None
        account['source_time_known'] = latest.get('source_time_known', True) if latest else False
        account['value'] = rates.convert(latest['value'], latest['currency'], currency_code, today.isoformat()) if latest else None
        if account.get('archived'):
            account['value'] = None
        account['estimated'] = bool(latest and (latest['estimated'] or latest['currency'] != currency_code))
        account['stale'] = bool(account['forced_stale'] or not latest or
            (today - date.fromisoformat(latest['date'])).days > (
                45 if account['source'] == 'statement' and not account.get('recorded_only_from') else 2))
        account['history_points'] = len(account['observations'])
        account['unreconciled'] = bool(latest and latest['source'] != 'manual' and not latest['reconciled'])

    # Resolve current credit positions once, retaining the original evidence.
    # The effective values must drive both account rows and headline totals.
    for account in accounts:
        if account['kind'] != 'credit':
            continue
        account['reported_native_value'] = account['native_value']
        account['reported_value'] = account['value']
        latest = current_observation(account)
        reported_source = latest['source'] if latest and latest['source'] in {'manual', 'lunchflow'} else 'statement'
        reported_as_of = (account.get('fetched_at') or account['as_of']) if reported_source == 'lunchflow' else account['as_of']
        valuation = resolve_credit_balance(conn, account['id'].removeprefix('statement:'),
            reported_balance=account['native_value'], source=reported_source, currency=account['currency'],
            reported_as_of=reported_as_of, institution=account['institution'], today=today)
        account.update(valuation=valuation, balance_check=valuation['balance_check'],
                       native_value=valuation['effective_value'], as_of=valuation['effective_as_of'])
        account['value'] = rates.convert(account['native_value'], account['currency'], currency_code, today.isoformat())
        account['estimated'] = account['estimated'] or valuation['estimated']

    for account in accounts:
        _project_ownership(account)

    observation_dates = {account['id']: [point['date'] for point in account['observations']]
                         for account in accounts}
    history_dates = {account['id']: [point['date'] for point in account.get('history_observations', account['observations'])]
                     for account in accounts}
    recorded_credit_by_day = {account['id']: {point['date']: point
        for point in account.get('debt_credit_observations', [])} for account in accounts}

    def total(when, cohort=None, *, require_complete=False, historical=False, investment_history=False):
        values, known, carried, estimated = [], [], 0, False
        observation_times = []
        bases, methods, reconstructed, source_break = set(), [], 0, False
        included = selected if cohort is None else cohort
        for account in included:
            projected = historical or (investment_history and account['kind'] == 'investment')
            observations = account.get('history_observations', account['observations']) if projected else account['observations']
            if when == today.isoformat() and account.get('valuation'):
                amount = account['value']
                if amount is not None:
                    values.append(amount)
                    known.append(account['id'])
                    observation_times.append(None)
                    carried += account['as_of'] != when
                    estimated = estimated or account['estimated']
                    basis = ('available_credit' if account['valuation']['basis'] == 'available_credit'
                             else 'recorded_balance')
                    bases.add(basis); methods.append(account['id'] + ':' + basis + ':0')
                continue
            exact_recorded_debt = recorded_credit_by_day[account['id']].get(when) if historical else None
            if exact_recorded_debt is not None:
                point = exact_recorded_debt
            else:
                if when == today.isoformat() and not projected:
                    # Current provider receipts remain usable across different
                    # UTC/workspace dates. Historical evidence keeps its saved
                    # source date and is selected by that date below.
                    point = current_observation(account)
                    if point is None:
                        continue
                else:
                    index = bisect_right(history_dates[account['id']] if projected else observation_dates[account['id']], when) - 1
                    if index < 0:
                        continue
                    point = observations[index]
            statement_fallback = False
            if account['kind'] == 'credit' and point['source'] == 'lunchflow':
                # Interpret the feed under the rule in force at its receipt.
                # Earlier reads with no confirmed rule are ambiguous, so keep
                # the statement fallback rather than use a later rule's limit.
                active_rule = None
                try:
                    fetched = datetime.fromisoformat(point['fetched_at'].replace('Z', '+00:00'))
                    for event in account.get('credit_rule_events', []):
                        confirmed = datetime.fromisoformat(event['confirmed_at'].replace('Z', '+00:00'))
                        if confirmed <= fetched:
                            active_rule = event['balance_type']
                        else:
                            break
                except (KeyError, TypeError, ValueError):
                    pass
                statement_fallback = (active_rule == 'available_credit' or
                    (active_rule is None and bool(account.get('credit_rule_events'))))
            if statement_fallback:
                # Available credit is not dated debt. Use the most recent
                # reconciled statement, including one hidden by a same-day
                # provider receipt; never apply today's credit limit to history.
                statements = account.get('debt_statement_observations', [])
                statement_dates = [item['date'] for item in statements]
                statement_index = bisect_right(statement_dates, when) - 1
                if statement_index < 0:
                    continue
                point = statements[statement_index]
            if projected and point.get('source') == 'reconstructed' and point['date'] != when:
                continue  # Missing replay days are gaps, never frozen positions.
            if (projected and account['kind'] == 'investment' and point.get('source') == 'statement'
                    and point['date'] != when):
                continue  # A reported investment NAV establishes that date only.
            if (projected and account.get('recorded_only_from') and when >= account['recorded_only_from']
                    and (point.get('source') != 'recorded' or point['date'] != when)):
                continue  # Only a dated broker observation establishes a live-history day.
            if historical and exact_recorded_debt is None and (not observations or when > observations[-1]['date']):
                continue
            if historical and (date.fromisoformat(when) - date.fromisoformat(point['date'])).days > (45 if point['source'] == 'statement' else 7):
                continue
            if account.get('archived') and exact_recorded_debt is None and (
                    not observations or when > observations[-1]['date'] or when == today.isoformat()):
                continue
            amount = rates.convert(point['value'], point['currency'], currency_code, when)
            if amount is None:
                continue
            values.append(amount)
            known.append(account['id'])
            observation_times.append(point.get('observed_at') or point.get('retrieved_at'))
            carried += point['date'] != when
            estimated = estimated or point['estimated'] or point['currency'] != currency_code or (
                statement_fallback and point['date'] != when)
            basis = point.get('valuation_basis') or ('provider_estimate' if point['source'] == 'snaptrade_estimate' else 'recorded_balance')
            bases.add(basis); methods.append(account['id'] + ':' + basis + ':' + str(point.get('comparison_segment', 0)))
            reconstructed += basis == 'daily_holdings'
            source_break = source_break or bool(point.get('source_break') and point['date'] == when)
        complete = bool(included) and len(known) == len(included)
        valued = bool(known) and (complete or not require_complete)
        # This clock is the last constituent receipt/observation, not a claim
        # that all broker quotes were simultaneous. Date-only inputs stay undated.
        observed_at = (max(observation_times) if observation_times and all(observation_times)
                       and all(t[:10] == when for t in observation_times) else None)
        return {'date': when, 'observed_at': observed_at, 'net_worth': round(sum(values), precision) if valued else None,
                'assets': round(sum(max(0, v) for v in values), precision) if valued else None,
                'liabilities': round(-sum(min(0, v) for v in values), precision) if valued else None,
                'known_accounts': len(known), 'missing_accounts': len(included) - len(known),
                'complete': complete, '_known': sorted(known),
                'estimated': estimated, 'carried_accounts': carried,
                'valuation_basis': next(iter(bases)) if len(bases) == 1 else 'mixed',
                'valuation_key': hashlib.sha256('|'.join(sorted(methods)).encode()).hexdigest()[:16],
                'reconstructed_accounts': reconstructed,
                'source_break': source_break,
                'coverage_key': hashlib.sha256('|'.join(sorted(known)).encode()).hexdigest()[:16]}

    all_dates = {p['date'] for a in selected for p in
                 [*a.get('history_observations', a['observations']), *a.get('debt_credit_observations', [])]}
    first_date = min({today.isoformat()} | all_dates)
    if cutoff is not None:
        first_date = max(first_date, cutoff.isoformat())
    # Daily replay must expose missing days; otherwise a line bridges unknown
    # shares/cash between statement periods. FX is sampled on each date too.
    if any(p.get('source') == 'reconstructed' for a in selected for p in a.get('history_observations', [])):
        start = date.fromisoformat(first_date)
        all_dates.update((start + timedelta(days=offset)).isoformat() for offset in range((today - start).days + 1))
    recorded_from = min((a['recorded_only_from'] for a in selected if a.get('recorded_only_from')), default=None)
    if recorded_from:
        start = date.fromisoformat(max(first_date, recorded_from))
        all_dates.update((start + timedelta(days=offset)).isoformat() for offset in range((today - start).days + 1))
    dates = sorted({d for d in all_dates if first_date <= d <= today.isoformat()} | {first_date, today.isoformat()})
    points = [total(d, investment_history=d != today.isoformat()) for d in dates]
    current = total(today.isoformat())
    # Prefer repeated coverage of the selected assets, not a long series of
    # small/empty accounts that silently drops the main funded brokerage.
    # Requiring repeated observations still excludes current-only accounts.
    historical_coverage = [total(when, historical=True) for when in dates]
    # Evaluate the whole selected period. A missing day creates a visible gap,
    # but must not erase older dates on which *every* selected account has a
    # supported value. The final point uses the exact headline calculation.
    start = date.fromisoformat(first_date)
    full_net_points = [({**current, 'net_worth': None, 'assets': None, 'liabilities': None}
                        if not current['complete'] else current)
                       if when == today else total(when.isoformat(), require_complete=True, historical=True)
                       for when in (start + timedelta(days=offset)
                                    for offset in range((today - start).days + 1))]
    first_supported = next((index for index, point in enumerate(full_net_points) if point['complete']), None)
    full_net_points = full_net_points[first_supported:] if first_supported is not None else []
    # An investment-only series can extend beyond complete net-assets coverage.
    # Every value still requires all selected investment accounts; missing bank
    # balances are never assumed to be zero or extrapolated from other PDFs.
    investments = [account for account in selected if account['kind'] == 'investment']
    investment_dates = [point['date'] for account in investments
                        for point in account.get('history_observations', account['observations'])]
    investment_points = []
    if investment_dates:
        investment_first = min(investment_dates)
        if cutoff is not None:
            investment_first = max(investment_first, cutoff.isoformat())
        investment_start = date.fromisoformat(investment_first)
        investment_points = [total(when.isoformat(), investments, require_complete=True, historical=True)
                             if when != today else total(when.isoformat(), investments, require_complete=True)
                             for when in (investment_start + timedelta(days=offset)
                                          for offset in range((today - investment_start).days + 1))]
        first_investment = next((index for index, point in enumerate(investment_points) if point['complete']), None)
        investment_points = investment_points[first_investment:] if first_investment is not None else []
    coverage = Counter(tuple(p['_known']) for p in historical_coverage if p['_known'])
    repeated = {a['id'] for a in selected if len({p['date'] for p in a.get('history_observations', a['observations'])
        if first_date <= p['date'] <= today.isoformat()
        and rates.convert(p['value'], p['currency'], currency_code, p['date']) is not None}) >= 2}
    candidates = [members for members, count in coverage.items() if count >= 2 and repeated.intersection(members)]
    current_magnitudes = {a['id']: abs(a['value'] or 0) for a in selected}
    magnitude = lambda members: sum(current_magnitudes[id] for id in members)
    if candidates:
        richest = max(candidates, key=magnitude)
        material_threshold = magnitude(richest) * .95
        # A few small accounts must not compress a year of investment history
        # into one month. Keep at least 95% of the richest repeated cohort's
        # current absolute balance, then prefer more jointly supported days.
        core, core_value = [], 0.
        for identifier in sorted(richest, key=lambda id: (-current_magnitudes[id], id)):
            core.append(identifier)
            core_value += current_magnitudes[identifier]
            if core_value >= material_threshold:
                break
        candidates.append(tuple(sorted(core)))
        supported = lambda members: sum(count for known, count in coverage.items() if set(members).issubset(known))
        candidates = [members for members in candidates if magnitude(members) >= material_threshold and supported(members) >= 2]
        cohort_ids = max(candidates, key=lambda members: (supported(members), magnitude(members), len(members), members))
    else:
        cohort_ids = ()
    cohort = [a for a in selected if a['id'] in cohort_ids]
    cohort_dates = {p['date'] for a in cohort for p in a.get('history_observations', a['observations']) if first_date <= p['date'] <= today.isoformat()}
    if (any(a.get('recorded_only_from') for a in cohort)
            or any(p.get('source') == 'reconstructed' for a in cohort for p in a.get('history_observations', []))):
        cohort_dates.update(dates)
    if cohort:
        cohort_dates.update((first_date, today.isoformat()))
        for account in cohort:
            observations = account.get('history_observations', account['observations'])
            for before, after in zip(observations, observations[1:]):
                days = (1 if account['kind'] == 'investment' and before['source'] == 'statement'
                        else 46 if before['source'] == 'statement' else 8)
                expiry = (date.fromisoformat(before['date']) + timedelta(days=days)).isoformat()
                if first_date <= expiry < after['date'] and expiry <= today.isoformat():
                    cohort_dates.add(expiry)
    cohort_points = [total(when, cohort, require_complete=True, historical=True) for when in sorted(cohort_dates)] if cohort else []
    # Retain today's recorded balance on the same timeline as older history.
    # Its different valuation key starts a separate chart segment; unsupported
    # intervening dates remain gaps rather than an invented continuation.
    valued_history = [point for point in cohort_points if point['complete']]
    current_separate = bool(len(valued_history) >= 3 and valued_history[-1]['date'] == today.isoformat()
        and valued_history[-1]['valuation_key'] not in {point['valuation_key'] for point in valued_history[:-1]}
        and max(Counter(point['valuation_key'] for point in valued_history[:-1]).values()) >= 2)
    # Leading/trailing gaps add no historical information. Interior gaps remain
    # explicit so a line cannot bridge missing accounts or exchange rates.
    while cohort_points and not cohort_points[0]['complete']:
        cohort_points.pop(0)
    while cohort_points and not cohort_points[-1]['complete']:
        cohort_points.pop()
    # Matching endpoints are insufficient: an interior gap or temporary
    # valuation-basis change must not disappear from the period comparison.
    history_comparable = (len(cohort_points) > 1
        and all(point['complete'] for point in cohort_points)
        and len({point['valuation_key'] for point in cohort_points}) == 1
        and not any(point['source_break'] for point in cohort_points))
    history_change = round(cohort_points[-1]['net_worth'] - cohort_points[0]['net_worth'], precision) if history_comparable else None
    cohort_current = total(today.isoformat(), cohort, require_complete=True) if cohort else None
    account_coverage = []
    for account in selected:
        included = account['id'] in cohort_ids
        current_value = account['value']
        reason = ('included' if included else 'current_value_unknown' if current_value is None else
                  'zero_balance' if current_value == 0 else 'insufficient_history' if account['id'] not in repeated else
                  'shorter_shared_history')
        # Explain the contribution to the final plotted total, using exactly
        # the same dated source and FX rules as the cohort calculation.
        endpoint = total(cohort_points[-1]['date'], [account], historical=True) if included and cohort_points else None
        account_coverage.append({
            'account_id': account['id'], 'included': included, 'reason': reason,
            'current_value': current_value,
            'latest_observation_date': account['as_of'],
            'history_end_date': endpoint['date'] if endpoint else None,
            'history_end_value': endpoint['net_worth'] if endpoint else None,
            'history_valuation_basis': endpoint['valuation_basis'] if endpoint else None,
            'provider_conflict_dates': account.get('history_provider_conflict', {}).get('dates', 0),
            'source_discontinuities': account.get('history_discontinuities', 0),
            **observation_coverage(account, first_date=first_date),
        })
    coverage_summary = {'included_accounts': len(cohort)}
    for scope, included in [('included', True), ('excluded', False)]:
        members = [account for account in account_coverage if account['included'] == included]
        coverage_summary.update({
            f'{scope}_nonzero_accounts': sum(account['current_value'] is not None and account['current_value'] != 0 for account in members),
            f'{scope}_zero_accounts': sum(account['current_value'] == 0 for account in members),
            f'{scope}_unknown_accounts': sum(account['current_value'] is None for account in members),
        })
    cohort_history = {
        'mode': 'comparable_cohort', 'account_ids': list(cohort_ids),
        'excluded_account_ids': [identifier for identifier in requested if identifier not in cohort_ids],
        'account_count': len(cohort), 'selected_accounts': len(selected),
        'start_date': cohort_points[0]['date'] if cohort_points else None,
        'end_date': cohort_points[-1]['date'] if cohort_points else None,
        'end_value': cohort_points[-1]['net_worth'] if cohort_points else None,
        'current_value': cohort_current['net_worth'] if cohort_current else None,
        'change': history_change,
        'change_pct': history_change / abs(cohort_points[0]['net_worth']) * 100
            if history_change is not None and cohort_points[0]['net_worth'] else None,
        'change_comparable': history_change is not None,
        'account_coverage': account_coverage, 'coverage_summary': coverage_summary,
        'current_shown_separately': current_separate,
        'continuity': history_continuity(cohort_points, cohort),
        'points': [{k: v for k, v in p.items() if k != '_known'} for p in cohort_points],
        'net_start_date': full_net_points[0]['date'] if full_net_points else None,
        'net_points': [{k: v for k, v in p.items() if k != '_known'} for p in full_net_points],
        'coverage_note': (f'Full net assets include all {len(selected)} selected accounts where dated values are supported, from {full_net_points[0]["date"]}. Missing days remain gaps.'
            if cohort and len(cohort) != len(selected) and full_net_points else
            'Some accounts lack older dated values. The current headline includes all supported selected balances.'
            if cohort and len(cohort) != len(selected) else
            'The trend tracks the same selected accounts through this period.' if cohort else
            'At least two dated observations with consistent account coverage are needed to draw a comparable trend.'),
    }
    # The public history contract is net assets for the complete selection.
    # Keep the older fixed-account calculation only as named coverage evidence.
    last_full = next((point for point in reversed(full_net_points) if point['complete']), None)
    net_comparable = (len(full_net_points) > 1 and all(point['complete'] for point in full_net_points)
        and len({point['valuation_key'] for point in full_net_points}) == 1
        and not any(point['source_break'] for point in full_net_points))
    net_change = round(last_full['net_worth'] - full_net_points[0]['net_worth'], precision) if net_comparable else None
    history = {
        'mode': 'net_assets', 'account_ids': [account['id'] for account in selected],
        'excluded_account_ids': [], 'account_count': len(selected), 'selected_accounts': len(selected),
        'start_date': full_net_points[0]['date'] if full_net_points else None,
        'end_date': last_full['date'] if last_full else None,
        'end_value': last_full['net_worth'] if last_full else None,
        'current_value': current['net_worth'] if current['complete'] else None,
        'change': net_change,
        'change_pct': net_change / abs(full_net_points[0]['net_worth']) * 100
            if net_change is not None and full_net_points[0]['net_worth'] else None,
        'change_comparable': net_change is not None,
        'points': [{k: v for k, v in point.items() if k != '_known'} for point in full_net_points],
        'net_start_date': full_net_points[0]['date'] if full_net_points else None,
        'net_points': [{k: v for k, v in point.items() if k != '_known'} for point in full_net_points],
        'investment_history': {
            'account_ids': [account['id'] for account in investments],
            'start_date': investment_points[0]['date'] if investment_points else None,
            'end_date': next((point['date'] for point in reversed(investment_points) if point['complete']), None),
            'points': [{k: v for k, v in point.items() if k != '_known'} for point in investment_points],
        },
        'coverage_note': (f'Every plotted value includes all {len(selected)} selected accounts. Missing dates remain gaps.'
                          if full_net_points else 'No date has supported balances for every selected account.'),
        'cohort': cohort_history,
    }
    # A constant account set is insufficient when valuation sources or their
    # comparison segments change. Such a jump is not a comparable value change.
    comparable = (history_comparable and len(points) > 1 and bool(selected) and all(p['missing_accounts'] == 0 for p in points)
        and len({p['coverage_key'] for p in points}) == 1
        and len({p['valuation_key'] for p in points}) == 1
        and not any(p['source_break'] for p in points)
        and not any(a.get('history_reconstruction') for a in selected))
    change = round(current['net_worth'] - points[0]['net_worth'], precision) if comparable else None
    summary = {key: current[key] for key in ('net_worth', 'assets', 'liabilities', 'known_accounts', 'missing_accounts')}
    stale = sum(a['stale'] for a in selected)
    balance_checks = [a for a in selected if a.get('balance_check', {}).get('requires_review')]
    estimated_accounts = sum(bool((a.get('valuation') or {}).get('estimated')) for a in selected)
    unknown_liabilities = sum(bool((a.get('valuation') or {}).get('current_unknown')) for a in selected)
    unmapped_banks = 0
    if account_ids is None and conn.execute("SELECT 1 FROM sqlite_master WHERE name='bank_connections'").fetchone():
        unmapped_banks = sum(lunchflow_store.connection_state(conn, row['provider_connection_id'])['enabled']
            for row in conn.execute('''SELECT provider_connection_id FROM bank_connections
                WHERE account_id IS NULL AND excluded=0 AND present=1'''))
    summary.update(selected_accounts=len(selected), stale_accounts=stale,
        unmapped_bank_accounts=unmapped_banks,
        balance_check_accounts=len(balance_checks),
        estimated_accounts=estimated_accounts, unknown_liability_accounts=unknown_liabilities,
        partial=bool(unmapped_banks or current['missing_accounts'] or stale or balance_checks or any(a['unreconciled'] for a in selected)),
        change=change, change_pct=(change / abs(points[0]['net_worth']) * 100) if comparable and points[0]['net_worth'] else None,
        change_comparable=comparable)
    warnings = list(reconstruction_issues)
    if unmapped_banks:
        warnings.append(f'{unmapped_banks} discovered bank accounts need linking or account setup in Banking. Their balances are not yet included in net assets.')
    warnings.extend(f"{account['name']}: {account['settings_warning']}" for account in selected if account.get('settings_warning'))
    for account in balance_checks:
        warnings.append(f"{account['name']}: {account['balance_check']['message']}")
    if current['missing_accounts']:
        warnings.append('Some selected accounts have no supported value or dated exchange rate. The headline is the known subtotal, not complete net worth.')
    if stale:
        warnings.append('Some balances are old or their connection is disabled. Source dates are shown below; refreshing a connection does not update statement-only bank balances.')
    if any(a['unreconciled'] for a in selected):
        warnings.append('Some imported balances have not reconciled against their transactions.')
    selected_brokers = {institution(a['institution']) for a in selected if a['source'] == 'snaptrade' and not a.get('archived')}
    if any(a['source'] == 'statement' and a['kind'] == 'investment' and institution(a['institution']) in selected_brokers for a in selected):
        warnings.append('An imported broker account and a connection at the same broker are selected. They may represent the same money; exclude one unless they are distinct accounts.')
    if any(a.get('exclusion_reason', '') and 'Paper' in a['exclusion_reason'] for a in selected):
        warnings.append('This selection includes simulated paper-account values.')
    has_investments = any(a['kind'] == 'investment' for a in selected)
    has_connected_investments = any(a['source'] == 'snaptrade' for a in selected)
    selected_connected_ids = {a['id'] for a in selected if a['source'] == 'snaptrade'}
    if cached is not None and has_connected_investments and any(
            'connected:' + str(a.get('id')) in selected_connected_ids
            and a.get('history', {}).get('status') in {'error', 'unsupported'}
            for a in cached.get('accounts', [])):
        warnings.append('Historical brokerage values are unavailable for some accounts. SnapTrade’s optional free account-history add-on may need enabling in its dashboard; local snapshots continue to build from now on.')
    if len(points) > 1 and not comparable:
        warnings.append('Historical account coverage changes across this period. Value changes are not shown as a gain or loss.')
    if cohort and len(cohort) != len(selected):
        warnings.append(f'Earlier history covers {len(cohort)} of {len(selected)} selected accounts. '
                        + (f'Full net assets begin on {full_net_points[0]["date"]}.' if full_net_points
                           else 'The current total uses every supported selected balance.'))
    methodology = [
        'Report dates use the workspace time zone. Provider observations retain their UTC source dates; a current balance can therefore have a different source calendar date.',
        'Net assets add the latest supported balances of selected accounts. Positive balances add to assets; debt and negative balances are subtracted. Accounts without a supported balance make the total partial.',
        'Change in account value includes contributions, withdrawals and market changes. It is not investment return.',
    ]
    has_credit = any(a['kind'] == 'credit' for a in selected)
    has_statements = any(p['source'] == 'statement' for a in selected for p in a['observations'])
    has_gbp_bank_statements = any(a['kind'] != 'investment' and a['currency'] == 'GBP'
                                  and p['source'] == 'statement'
                                  for a in selected for p in a['observations'])
    has_lunchflow = any(a['source'] == 'lunchflow' for a in selected)
    if has_credit:
        methodology.append('Credit balances are subtracted; an overpaid card is an asset. Pending activity is not added to the reported balance again.')
        if any(a.get('provider_balance_type') == 'available_credit' for a in selected):
            methodology.append('For a confirmed available-credit feed, current debt equals the confirmed credit limit minus available credit. Historic available-credit observations are not converted using today’s limit; a reconciled statement balance may be carried for up to 45 days and is marked estimated.')
        if any(a.get('balance_check', {}).get('requires_review') for a in selected if a['kind'] == 'credit'):
            methodology.append('A conflicting card balance may use a labelled statement-plus-booked-activity estimate when coverage is sufficient. Otherwise the current balance remains unknown rather than zero.')
    if has_investments:
        methodology.append('Brokerage account totals already include cash and any margin borrowing; holdings and cash are not added again.')
        methodology.append('Historical investment values use supported holdings, prices, cash movements and dated exchange rates. Missing evidence creates gaps; reconstructed values are estimates.')
        if any((a.get('history_provider_conflict') or {}).get('dates', 0) > 0 for a in selected):
            methodology.append('Conflicting provider history is excluded when it differs materially from supported reconstruction. Current totals still use broker-reported balances.')
        if any(a['source'] == 'snaptrade' for a in selected):
            methodology.append('From each connected account’s first saved SnapTrade receipt, recorded broker balances establish daily history. Unrecorded days remain gaps.')
    if has_statements:
        methodology.append('Statement balances are dated observations. Bank balances can carry forward for at most 45 days; investment statement values apply only on their reported dates.')
    if has_gbp_bank_statements:
        methodology.append('Reconciled GBP bank statements can add printed transaction-day balances when their complete transaction chain agrees with the closing balance.')
    if has_lunchflow:
        methodology.append('Lunch Flow balance dates are fetch dates, which may differ from the bank’s update time. Linked statement history remains available.')
    if any(a['currency'] != currency_code for a in selected):
        methodology.append('Foreign amounts use dated ECB reference rates, carried forward for at most seven days. Missing rates remain unknown; conversions are indicative.')
    if cohort:
        methodology.append('Earlier account history holds one repeatedly observed account set fixed. Gaps and changes in valuation source are not shown as gains or losses.')
    if full_net_points:
        methodology.append('Full net-assets history shows every date with supported values for all selected accounts. Unsupported days remain gaps; older balances are not filled using today’s amount.')
    if any(a.get('linked_to') for a in accounts):
        methodology.append('Linked account sources are counted once. Their original statements, transactions and provider observations remain intact.')
    if any(a.get('archived') for a in accounts):
        methodology.append('Archived sources are excluded from the default selection. Saved records remain available, and explicitly selected sources contribute their supported balance.')
    return {'generated_at': datetime.now(timezone.utc).isoformat(), 'currency': currency_code,
        'currencies': currencies, 'range': range_name, 'selected_account_ids': requested,
        'accounts': [{k: v for k, v in a.items() if k not in ('observations', 'history_observations',
                    'debt_statement_observations', 'debt_credit_observations', 'credit_rule_events',
                    'forced_stale', 'unreconciled')} for a in accounts],
        'summary': summary, 'history': history, 'points': [{k: v for k, v in p.items() if k != '_known'} for p in points],
        'warnings': warnings, 'methodology': methodology,
        'cache': {k: (cached or {}).get('cache', {}).get(k) for k in ('saved_at', 'stale', 'refresh_error')}}


def refresh_fx(conn, *, path=None, today=None, fetch=None):
    """Only public currency/date pairs leave Am I Broke?; no account identifiers."""
    from concurrent.futures import ThreadPoolExecutor
    from .prices import batch_reference_fx
    provider_today = today or datetime.now(timezone.utc).date()
    today = today or reporting_today(conn)
    accounts, cached = sources(conn, path=path, today=today, provider_today=provider_today)
    codes = {p['currency'] for a in accounts for p in a['observations']}
    # Account base currency does not describe every holding or cash balance.
    # Sector/session values also need the verified listing's native currency.
    for account in (cached or {}).get('accounts', []):
        for row in account.get('positions', {}).get('data', []):
            codes.update((currency(row.get('currency')), currency(row.get('listing_currency'))))
        codes.update(currency(row.get('currency')) for row in account.get('balances', {}).get('data', []))
    # Closed holdings and cash currencies can be absent from today's feed.
    for row in conn.execute("SELECT s.meta FROM statements s JOIN accounts a ON a.id=s.account_id WHERE a.account_type='investment'"):
        meta = metadata(row['meta'])
        for item in meta.get('positions', []) + meta.get('position_changes', []):
            codes.add(currency(item.get('currency')))
        for field in ('cash_balances', 'opening_cash_balances'):
            codes.update(currency(code) for code, amount in (meta.get(field) or {}).items() if number(amount) not in (None, 0))
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='transactions' AND type='table'").fetchone():
        for row in conn.execute("SELECT t.meta FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE a.account_type='investment'"):
            meta = metadata(row['meta'])
            codes.update(currency(meta.get(field)) for field in ('native_currency', 'execution_currency_major'))
            codes.update(currency(code) for code, amount in (meta.get('cash_movements') or {}).items() if number(amount) not in (None, 0))
    currencies = sorted(code for code in codes if code not in (None, 'GBP'))[:8]
    from .workspace import preferences
    from .currency_reporting import CURRENCIES
    target = preferences(conn)['reporting_currency']
    # Keep the account-discovery budget without dropping the reporting target
    # when a household has many different source currencies.
    if target in CURRENCIES and target != 'GBP' and target not in currencies:
        currencies.append(target)
    boundaries = {(today - timedelta(days=offset)).isoformat() for offset in RANGES.values() if offset}
    dates = sorted({today.isoformat()} | boundaries | {p['date'] for a in accounts for p in a['observations']
        if p['date'] >= date(today.year - 4, 1, 1).isoformat()})
    # Replaying shares against daily market prices also needs each day's FX,
    # rather than only the dates on which a broker returned an account value.
    periods = conn.execute("SELECT MIN(s.period_start) FROM statements s JOIN accounts a ON a.id=s.account_id WHERE a.account_type='investment'").fetchone()
    if periods and day(periods[0]):
        first = max(date.fromisoformat(day(periods[0])), today - timedelta(days=730))
        dates = sorted(set(dates) | {(first + timedelta(days=offset)).isoformat() for offset in range((today - first).days + 1)})
    existing = {(r['currency'], r['date']) for r in conn.execute('SELECT currency,date,observed_date FROM reference_fx')
                if r['date'] != today.isoformat() or r['observed_date'] == r['date']}
    grouped = defaultdict(list)
    for code in currencies:
        for when in dates:
            if (code, when) not in existing:
                grouped[(code, when[:4])].append((code, when))
    def run(items):
        try:
            return (fetch or batch_reference_fx)(items)
        except Exception:
            return {}
    updated, attempted = 0, sum(len(items) for items in grouped.values())
    with ThreadPoolExecutor(max_workers=4) as pool:
        for results in pool.map(run, grouped.values()):
            for (_, _), row in results.items():
                rate = number(row.get('rate_to_gbp'))
                if rate is None or rate <= 0:
                    continue
                conn.execute('INSERT OR REPLACE INTO reference_fx(date,currency,rate_to_gbp,source,observed_date,fetched_at,source_url) VALUES(?,?,?,?,?,?,?)',
                    (row['date'], row['currency'], rate, row['source'], row['observed_date'], row['fetched_at'], row.get('source_url')))
                updated += 1
    conn.commit()
    return {'updated': updated, 'attempted': attempted, 'missing': attempted - updated,
            'message': 'Dated exchange rates updated.' if updated == attempted else 'Some dated exchange rates are unavailable; affected values remain unknown.'}


def scheduled_fx_refresh():
    """Refresh public reference rates and prices alongside portfolio reads."""
    from . import db
    if not db.DB_PATH.exists():
        return
    conn = db.connect(db.DB_PATH)
    try:
        refresh_fx(conn, path=db.DB_PATH)
    finally:
        conn.close()
    from .market_cache import refresh_market_data
    refresh_market_data(path=db.DB_PATH, include_benchmarks=True)

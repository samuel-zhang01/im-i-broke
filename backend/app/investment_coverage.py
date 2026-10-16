"""Cached investment coverage, with source dates distinct from retrieval dates."""
from datetime import datetime, timezone

from . import snaptrade_store
from .snaptrade_models import currency, number


SECTIONS = ('balances', 'positions', 'activities', 'orders', 'history', 'return_rates')


def _date(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _range(rows, key):
    dates = sorted(value for row in rows if (value := _date(row.get(key))) is not None)
    return (dates[0].date().isoformat(), dates[-1].date().isoformat()) if dates else (None, None)


def bundle(conn, *, path=None, currency_code='GBP', account_ids=None, paper=False, cached=None, now=None):
    if not currency(currency_code):
        raise ValueError('Choose a supported reporting currency.')
    now = now or datetime.now(timezone.utc)
    now = _date(now)
    if now is None:
        raise ValueError('Choose a valid observation date.')
    if cached is None:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='snaptrade_cache' AND type='table'").fetchone()
        cached = snaptrade_store._load(conn) if exists else None
    cached = cached or {'accounts': [], 'connections': {'data': []}, 'cache': {}}
    accounts = cached.get('accounts', [])
    requested = snaptrade_store.selected_account_ids(accounts, account_ids, paper=paper)
    selected = [account for account in accounts if account['id'] in requested]
    modes = {bool(account.get('is_paper')) for account in selected}
    if len(modes) > 1:
        raise ValueError('View live and paper accounts separately.')
    from .investment_activity import list_activity_coverage
    archive = list_activity_coverage(conn, account_ids=requested)
    disabled = {row['id'] for row in cached.get('connections', {}).get('data', []) if row.get('disabled')}
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result = []
    for account in selected:
        sections = []
        for key in SECTIONS:
            section = account.get(key) or {}
            rows = section.get('data') or []
            first, last = _range(rows, 'as_of' if key == 'return_rates' else 'date')
            source_at = (account.get('holdings_synced_at') if key in {'balances', 'positions'}
                         else account.get('activities_synced_at') if key == 'activities'
                         else section.get('as_of'))
            if section.get('stale'):
                # New account metadata cannot date retained rows from an older
                # failed section. Use only that section's own source evidence.
                source_at = section.get('as_of')
            parsed = _date(source_at)
            age = (now.date() - parsed.date()).days if parsed else None
            sections.append({
                'key': key, 'status': section.get('status', 'unsupported'), 'rows': len(rows),
                'retained': bool(section.get('stale')), 'truncated': bool(section.get('truncated')),
                'source_at': source_at, 'source_age_days': age,
                'source_outdated': age is not None and (age > 2 or age < 0),
                'retrieved_at': section.get('cached_at') or (None if section.get('stale') else cached.get('retrieved_at')),
                'first_date': first, 'last_date': last, 'error': section.get('error'),
            })
        observations = []
        if 'snaptrade_account_observations' in tables:
            observations = [dict(row) for row in conn.execute('''
                SELECT retrieved_at, observed_at, source_time_known, holdings_status, holdings_truncated
                FROM snaptrade_account_observations WHERE account_id=?
                ORDER BY retrieved_at DESC LIMIT 8''', (account['id'],))]
        result.append({
            'id': account['id'], 'name': account.get('name'), 'institution': account.get('institution'),
            'initial_sync_complete': account.get('initial_sync_complete'),
            'is_paper': bool(account.get('is_paper')), 'connection_disabled': account.get('connection_id') in disabled,
            'account_value_known': number(account.get('total_value')) is not None,
            'sections': sections, 'activity_archive': archive.get(account['id']),
            'observations': observations,
        })
    return {
        'generated_at': now.isoformat(), 'retrieved_at': cached.get('retrieved_at'),
        'selected_account_ids': requested, 'paper': next(iter(modes)) if modes else paper,
        'accounts': result,
        'performance': {'personal_return_available': False, 'reason':
            'Complete dated external cash flows and comparable account valuations have not been verified. '
            'Saving activity protects future history; it does not establish lifetime coverage.'},
        'methodology': [
            'This view reads saved observations and makes no brokerage requests.',
            'Source sync dates describe broker observations; retrieval dates describe when Am I Broke? received data. Missing source dates remain unknown.',
            'An empty response means no records were returned for that request. Unsupported data is not a zero balance.',
            'Fully fetched activity windows describe successful pagination of requested dates, not proof that the broker supplied every historical transaction.',
            'Saved account observations and activity request windows retain their actual dates. No unseen dates are filled.',
        ],
    }

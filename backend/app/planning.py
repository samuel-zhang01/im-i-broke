"""Explainable planning from observed balances and complete statement coverage.

Scenarios are deterministic illustrations, not estimates of market probability.
Amounts are GBP. Percentage inputs use percentage points (5 means 5%).
"""
from __future__ import annotations
import calendar
import json
import math
import re
import statistics
from datetime import date, datetime, timedelta, timezone
from . import analytics
from .workspace import reporting_today, semantic


SCENARIO_DEFAULTS = {
    'years': 10, 'annual_return': 5.0, 'inflation': 2.0,
    'monthly_contribution': None, 'initial_wealth': None, 'target': 100000.0,
    'baseline_method': 'mean', 'exclude_outliers': False,
    'one_off_amount': 0.0, 'one_off_month': 1,
}


def _scenario_parameters(parameters):
    if not isinstance(parameters, dict) or set(parameters)-set(SCENARIO_DEFAULTS):
        raise ValueError('Scenario parameters contain unsupported fields')
    params = {**SCENARIO_DEFAULTS, **parameters}
    if (not isinstance(params['baseline_method'], str) or params['baseline_method'] not in {'mean', 'median'}
            or not isinstance(params['exclude_outliers'], bool)):
        raise ValueError('Choose mean or median and a boolean outlier setting')
    for field in ('years', 'one_off_month'):
        if not isinstance(params[field], int) or isinstance(params[field], bool):
            raise ValueError(f'{field} must be a whole number')
    limits = {'years': (1, 50), 'annual_return': (-50, 50), 'inflation': (0, 20),
              'monthly_contribution': (-1000000, 1000000), 'initial_wealth': (-1e9, 1e9),
              'target': (0.01, 1e10), 'one_off_amount': (0, 1e9), 'one_off_month': (1, params['years']*12)}
    for field, (low, high) in limits.items():
        value = params[field]
        if value is None and field in {'monthly_contribution', 'initial_wealth'}:
            continue
        # Compare bounded numeric values before converting arbitrarily large
        # JSON integers in isfinite(), which can otherwise raise OverflowError.
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high or not math.isfinite(value):
            raise ValueError(f'Invalid {field}')
    return params


def saved_scenarios(conn):
    return [{'id': row['id'], 'name': row['name'], 'parameters': json.loads(row['parameters']),
             'created_at': row['created_at'], 'updated_at': row['updated_at']}
            for row in conn.execute('SELECT * FROM planning_scenarios ORDER BY updated_at DESC,id DESC')]


def save_scenario(conn, name, parameters, scenario_id=None):
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
        raise ValueError('Scenario name must contain 1 to 100 characters')
    params = _scenario_parameters(parameters)
    now = datetime.now(timezone.utc).isoformat()
    if scenario_id is None:
        cur = conn.execute('INSERT INTO planning_scenarios(name,parameters,created_at,updated_at) VALUES(?,?,?,?)',
                           (name.strip(), json.dumps(params, allow_nan=False), now, now))
        scenario_id = cur.lastrowid
    else:
        cur = conn.execute('UPDATE planning_scenarios SET name=?,parameters=?,updated_at=? WHERE id=?',
                           (name.strip(), json.dumps(params, allow_nan=False), now, scenario_id))
        if not cur.rowcount:
            raise ValueError('Saved scenario does not exist')
    row = conn.execute('SELECT * FROM planning_scenarios WHERE id=?', (scenario_id,)).fetchone()
    saved = {'id': row['id'], 'name': row['name'], 'parameters': json.loads(row['parameters']),
             'created_at': row['created_at'], 'updated_at': row['updated_at']}
    conn.commit()
    return saved


def delete_scenario(conn, scenario_id):
    deleted = conn.execute('DELETE FROM planning_scenarios WHERE id=?', (scenario_id,)).rowcount
    conn.commit()
    return bool(deleted)


def account_snapshot(conn, today=None):
    today = today or reporting_today(conn)
    from .lunchflow_store import latest_balances
    from .wealth import Rates
    bank_balances = latest_balances(conn, today=today)
    rates = Rates(conn)
    from .account_settings import effective_settings
    from .workspace import exists
    accounts = []
    for a in conn.execute("SELECT * FROM accounts ORDER BY bank,account_type,id"):
        settings = effective_settings(conn, a['id'])
        s = conn.execute("SELECT * FROM statements WHERE account_id=? ORDER BY period_end DESC,id DESC LIMIT 1", (a['id'],)).fetchone()
        balance = s['closing_balance'] if s else None
        if a['account_type'] == 'investment':
            balance = conn.execute("SELECT SUM(amount) FROM transactions WHERE account_id=?", (a['id'],)).fetchone()[0]
        elif a['account_type'] == 'credit' and balance is not None:
            balance = -balance
        asof = s['period_end'] if s else None
        code = dict(a).get('currency') or 'GBP'
        native_balance = balance
        reported_native_balance = balance
        valuation = None
        source, fetched_at, stale = 'statement', None, False
        connected = bank_balances.get(a['id'])
        if connected and (not asof or connected['fetched_at'][:10] >= asof):
            native_balance = connected['amount']
            reported_native_balance = connected.get('reported_amount', native_balance)
            valuation = connected.get('valuation')
            code = connected['currency']
            balance = rates.convert(native_balance, code, 'GBP', today.isoformat())
            asof = connected.get('as_of') or connected['fetched_at'][:10]
            source, fetched_at = 'lunchflow', connected['fetched_at']
            stale = bool(connected.get('stale') or connected.get('error'))
        elif code != 'GBP':
            balance = rates.convert(native_balance, code, 'GBP', today.isoformat())
        if settings['origin'] == 'manual' and exists(conn, 'manual_balance_observations'):
            manual = conn.execute('SELECT * FROM manual_balance_observations WHERE account_id=? AND as_of<=? ORDER BY as_of DESC,id DESC LIMIT 1',
                                  (a['id'], today.isoformat())).fetchone()
            if manual and (not asof or manual['as_of'] >= asof):
                native_balance = reported_native_balance = manual['amount']
                code, asof, source, fetched_at = manual['currency'], manual['as_of'], 'manual', manual['recorded_at']
                balance = rates.convert(native_balance, code, 'GBP', asof)
                valuation, stale = None, False
        # Resolve the final selected observation, not a superseded statement.
        if a['account_type'] == 'credit' and source != 'lunchflow':
            from .balance_resolution import resolve_credit_balance
            valuation = resolve_credit_balance(conn, a['id'], reported_balance=native_balance,
                source=source, currency=code, reported_as_of=asof, institution=a['bank'], today=today)
            native_balance = valuation['effective_value']
            balance = rates.convert(native_balance, code, 'GBP', asof if source == 'manual' else today.isoformat())
            asof = valuation['effective_as_of']
        if balance is not None:
            balance *= settings['ownership_share']
        accounts.append({'id': a['id'], 'name': a['display_name'], 'type': a['account_type'],
                         'include_in_reports': settings['include_in_reports'] and not settings['archived'],
                         'include_in_planning': settings['include_in_planning'] and not settings['archived'],
                         'ownership_share': settings['ownership_share'],
                         'balance': round(balance, 2) if balance is not None else None,
                         'native_balance': native_balance, 'currency': code,
                         'reported_native_balance':reported_native_balance, 'valuation':valuation,
                         'balance_check':valuation.get('balance_check') if valuation else None,
                         'balance_source': source, 'balance_fetched_at': fetched_at, 'balance_stale': stale,
                         'balance_asof': asof, 'age_days': (today-date.fromisoformat(asof)).days if asof else None,
                         'reconciled': (None if source in {'lunchflow', 'manual'} else bool(s['reconciled']) if s and s['reconciled'] is not None else None)})
    return accounts


def _month_start(d, delta=0):
    n = d.year*12 + d.month-1 + delta
    return date(n//12, n%12+1, 1)


def _covers(intervals, start, end):
    cursor = start
    for left, right in sorted(intervals):
        if right < cursor:
            continue
        if left > cursor:
            return False
        if right >= end:
            return True
        cursor = max(cursor, right + timedelta(days=1))
        if cursor > end:
            return True
    return False


def covered_baseline(conn, today=None, window=6, *, exclude_outliers=False, baseline_method="mean", cashflow=None):
    """Last six eligible complete calendar months, within the trailing year.

    A month needs continuous, reconciled statements for every imported spending
    account. Coverage gaps never become zero-spending months. Investment cash
    income is removed from contributions because scenario growth models returns.
    """
    today = today or reporting_today(conn)
    if baseline_method not in {"mean", "median"}:
        raise ValueError("Baseline method must be mean or median")
    from . import account_coverage
    accounts, intervals, gaps, coverage_sources = account_coverage.state(conn)
    eligible_accounts = {a['id'] for a in accounts}
    from .account_settings import effective_settings
    for row in conn.execute("SELECT id FROM accounts WHERE account_type='investment'"):
        settings = effective_settings(conn, row['id'])
        if settings['include_in_reports'] and settings['include_in_planning'] and not settings['archived']:
            eligible_accounts.add(row['id'])
    cashflow = cashflow if cashflow is not None else analytics.cashflow_rows(conn)
    cashflow = [row for row in cashflow if row['account_id'] in eligible_accounts]
    # Coverage establishes completeness, not support for valuing foreign cash
    # flows. Native source rows must never disappear into a verified GBP zero.
    foreign_months = {}
    _, catalog = analytics.category_maps(conn)
    for raw in conn.execute("SELECT t.*,a.account_type FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE COALESCE(t.currency,'GBP')<>'GBP' AND t.txn_date IS NOT NULL"):
        row = dict(raw)
        if row['account_id'] not in eligible_accounts:
            continue
        if row['account_type'] == 'investment':
            analytics.annotate_category(row, catalog)
            component = analytics._household_component(row)
            if component is None or component['amount'] >= 0 and not component['is_refund']:
                continue  # Trades/transfers and investment income do not fund contributions.
        foreign_months.setdefault(row['txn_date'][:7], set()).add(row['account_id'])
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='bank_transactions'").fetchone():
        for row in conn.execute('''SELECT c.account_id,b.txn_date FROM bank_transactions b
            JOIN bank_connections c ON c.id=b.connection_id
            WHERE c.excluded=0 AND b.is_pending=0 AND b.status<>'ignored'
            AND b.currency<>'GBP' AND b.txn_date IS NOT NULL'''):
            if row['account_id'] in eligible_accounts:
                foreign_months.setdefault(row['txn_date'][:7], set()).add(row['account_id'])
    actual = {p['m']: p for p in analytics._aggregate_months(cashflow)}
    outliers = [r for r in analytics.detect_anomalies(conn, z=3.5, include_resolved=True,
                spending_rows=[r for r in cashflow if r['amount'] < 0]) if r.get('kind') == 'large_amount'] if exclude_outliers else []
    excluded_ids = {r['id'] for r in outliers}
    monthly = ({p['m']: p for p in analytics._aggregate_months([r for r in cashflow if r['id'] not in excluded_ids])}
               if exclude_outliers else actual)
    bank_interest = {}
    for row in cashflow:
        if row['account_type'] != 'investment' and row['amount'] > 0 and not row['is_refund'] and re.search(r'\binterest\b', row['description'] or '', re.I):
            key = row['txn_date'][:7]
            bank_interest[key] = bank_interest.get(key, 0) + row['amount']
    names = {row['id']: row['display_name'] or row['id'] for row in conn.execute('SELECT id,display_name FROM accounts')}
    included, excluded = [], []
    for offset in range(-12, 0):
        start = _month_start(today, offset)
        end = date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])
        key = start.strftime('%Y-%m')
        unsupported_ids = {a['id'] for a in accounts
                           if a['currency'] != 'GBP' and account_coverage.required_period(a, start, end)}
        unsupported_ids.update(foreign_months.get(key, set()))
        unsupported = [names[ident] for ident in sorted(unsupported_ids)]
        if unsupported:
            excluded.append({'month': key, 'reason': 'Foreign-currency activity is not valued by the GBP planning baseline: ' + ', '.join(unsupported)})
            continue
        missing, required_count = account_coverage.missing(accounts, intervals, gaps, start, end)
        if not required_count or missing:
            excluded.append({'month': key, 'reason': 'Incomplete verified coverage: ' + ', '.join(missing or ['no accounts'])})
            continue
        p = monthly.get(key, {})
        income = p.get('income', 0)-p.get('investment_income', 0)-bank_interest.get(key, 0)
        spending = p.get('spend', 0)
        recorded_spending = actual.get(key, {}).get('spend', 0)
        included.append({'month': key, 'income': round(income, 2), 'spending': round(spending, 2),
                         'surplus': round(income-spending, 2), 'recorded_spending': round(recorded_spending, 2),
                         'recorded_surplus': round(income-recorded_spending, 2),
                         'excluded_outlier_spend': round(recorded_spending-spending, 2),
                         'excluded_outlier_count': sum(r['txn_date'][:7] == key for r in outliers)})
    included = included[-window:]
    n = len(included)
    income = sum(p['income'] for p in included)
    spending = sum(p['spending'] for p in included)
    median_surplus = round(statistics.median(p['surplus'] for p in included), 2) if n else None
    avg_surplus = round((income-spending)/n, 2) if n else None
    return {'months': n, 'period_start': included[0]['month'] if n else None,
            'period_end': included[-1]['month'] if n else None,
            'avg_income': round(income/n, 2) if n else None,
            'avg_spending': round(spending/n, 2) if n else None,
            'avg_surplus': avg_surplus,
            'median_surplus': median_surplus,
            'median_income': round(statistics.median(p['income'] for p in included), 2) if n else None,
            'median_spending': round(statistics.median(p['spending'] for p in included), 2) if n else None,
            'recorded_avg_surplus': round(sum(p['recorded_surplus'] for p in included)/n, 2) if n else None,
            'recorded_avg_spending': round(sum(p['recorded_spending'] for p in included)/n, 2) if n else None,
            'excluded_outlier_spend': round(sum(p['excluded_outlier_spend'] for p in included), 2),
            'excluded_outlier_count': sum(p['excluded_outlier_count'] for p in included),
            'selected_contribution': median_surplus if baseline_method == 'median' else avg_surplus,
            'baseline_method': baseline_method, 'exclude_outliers': exclude_outliers,
            'savings_rate': (income-spending)/income if income > 0 else None,
            'monthly': included, 'excluded_months': excluded, 'coverage_sources': coverage_sources}


def compound_scenario(initial, monthly, annual_return, inflation, years, target, *,
                      one_off_amount=0.0, one_off_month=1):
    """Effective monthly compounding, fixed end-of-month nominal contributions."""
    if not all(math.isfinite(v) for v in (initial, monthly, annual_return, inflation, years, target, one_off_amount, one_off_month)):
        raise ValueError('Scenario inputs must be finite')
    if annual_return <= -100 or inflation <= -100 or not 1 <= years <= 50:
        raise ValueError('Invalid scenario rate or duration')
    if not isinstance(years, int) or isinstance(years, bool) or not isinstance(one_off_month, int) or isinstance(one_off_month, bool):
        raise ValueError('Years and planned expense month must be whole numbers')
    if one_off_amount < 0 or not 1 <= one_off_month <= years*12:
        raise ValueError('Planned expense must be non-negative and fall within the scenario')
    rate = math.expm1(math.log1p(annual_return/100)/12)
    value = initial
    points = [{'year': 0, 'nominal': round(initial, 2), 'real': round(initial, 2), 'contributed': round(initial, 2), 'planned_expenses': 0.0}]
    target_month = 0 if initial >= target else None
    for month in range(1, years*12+1):
        value = value*(1+rate)+monthly
        if month == one_off_month:
            value -= one_off_amount
        if target_month is None and value >= target:
            target_month = month
        if month % 12 == 0:
            points.append({'year': month//12, 'nominal': round(value, 2),
                           'real': round(value/(1+inflation/100)**(month/12), 2),
                           'contributed': round(initial+monthly*month-(one_off_amount if month >= one_off_month else 0), 2),
                           'planned_expenses': round(one_off_amount if month >= one_off_month else 0, 2)})
    return {'annual_return': annual_return, 'points': points, 'target_month': target_month,
            'one_off_amount': one_off_amount, 'one_off_month': one_off_month,
            'ending_above_target': value >= target}


def planning_bundle(conn, *, years=10, annual_return=5.0, inflation=2.0,
                    monthly_contribution=None, initial_wealth=None, target=100000.0, today=None,
                    baseline_method="mean", exclude_outliers=False, one_off_amount=0.0, one_off_month=1):
    _scenario_parameters({'years': years, 'annual_return': annual_return, 'inflation': inflation,
                          'monthly_contribution': monthly_contribution, 'initial_wealth': initial_wealth,
                          'target': target, 'baseline_method': baseline_method, 'exclude_outliers': exclude_outliers,
                          'one_off_amount': one_off_amount, 'one_off_month': one_off_month})
    today = today or reporting_today(conn)
    accounts = [a for a in account_snapshot(conn, today)
                if a.get('include_in_reports', True) and a.get('include_in_planning', True)]
    eligible_ids = {a['id'] for a in accounts}
    hv = analytics.household_holdings_value(conn, planning=True)
    unknown = [a['name'] for a in accounts if a['balance'] is None]
    balances = [a['balance'] for a in accounts if a['balance'] is not None]
    cash = sum(max(a['balance'] or 0, 0) for a in accounts if a['type'] != 'investment')
    liquid_cash = sum(max(a['balance'] or 0, 0) for a in accounts if a['type'] not in ('investment', 'credit'))
    inv_cash = sum(max(a['balance'] or 0, 0) for a in accounts if a['type'] == 'investment')
    debt = sum(-v for v in balances if v < 0)
    foreign_values = [c['value_gbp'] for c in hv.get('foreign_cash', []) if c.get('value_gbp') is not None]
    inv_cash += sum(max(v, 0) for v in foreign_values)
    debt += sum(-v for v in foreign_values if v < 0)
    stale = sum(a['age_days'] is None or a['age_days'] > 45 for a in accounts)
    source_rows = [row for row in analytics._rows(conn) if row['account_id'] in eligible_ids]
    cashflow = [component for row in source_rows if (component := analytics._household_component(row)) is not None]
    spending = [row for row in cashflow if row['amount'] < 0]
    untracked_fx = any(analytics._exclusion_reason(r) == 'Currency conversions' for r in source_rows)
    partial = bool(unknown or hv.get('n_unpriced', 0) or stale or hv.get('n_stale', 0) or hv.get('foreign_cash_unvalued', 0) or hv.get('foreign_cash_stale', 0) or untracked_fx or not hv.get('position_complete', True) or any(a['reconciled'] is not True for a in accounts))
    wealth = {'known_net_worth': round(sum(balances)+hv['market_value']+hv.get('foreign_cash_value', 0), 2),
              'cash_assets': round(cash, 2), 'investment_cash': round(inv_cash, 2),
              'holdings_value': hv['market_value'], 'liabilities': round(debt, 2),
              'liquid_after_debt': round(liquid_cash-debt, 2), 'unknown_balance_accounts': unknown,
              'unpriced_holdings': hv['n_unpriced'], 'stale_accounts': stale,
              'is_partial': partial, 'accounts': accounts, 'foreign_cash': hv.get('foreign_cash', []), 'foreign_cash_unvalued': hv.get('foreign_cash_unvalued', 0),
              'foreign_cash_value': hv.get('foreign_cash_value', 0),
              'foreign_currency_movements_unvalued': untracked_fx}
    baseline = covered_baseline(conn, today, exclude_outliers=exclude_outliers, baseline_method=baseline_method, cashflow=cashflow)
    scope = 'account_id IN (' + ','.join('?' for _ in eligible_ids) + ')' if eligible_ids else '0'
    scoped_ids = sorted(eligible_ids)
    q = conn.execute("SELECT SUM(reconciled=1),SUM(reconciled=0),SUM(reconciled IS NULL) FROM statements WHERE " + scope, scoped_ids).fetchone()
    uncat_rows = [r for r in cashflow if semantic(r) in {None, '', 'Uncategorized'}]
    uncat = (len(uncat_rows), sum(-r['amount'] for r in uncat_rows if r['amount'] < 0 or r['is_refund']))
    quality = {'verified_statements': q[0] or 0, 'failed_statements': q[1] or 0,
               'unverified_statements': q[2] or 0, 'uncategorized_count': uncat[0],
               'uncategorized_spend': round(uncat[1], 2),
               'undated_transactions': conn.execute("SELECT COUNT(*) FROM transactions WHERE txn_date IS NULL AND " + scope, scoped_ids).fetchone()[0]}
    # Cash buffers describe the real historical cost base, even when the user
    # chooses an adjusted contribution scenario that sets outliers aside.
    spend = baseline['recorded_avg_spending']
    rec = analytics.detect_recurring(conn, as_of=today,source_rows=source_rows,spending_rows=spending)
    resilience = {'runway_months': round(max(liquid_cash-debt, 0)/spend, 1) if spend and spend > 0 else None,
                  'three_month_buffer': round(spend*3, 2) if spend is not None and spend > 0 else None,
                  'six_month_buffer': round(spend*6, 2) if spend is not None and spend > 0 else None,
                  'recurring_monthly': round(sum(r['monthly_cost'] for r in rec if r['status'] in ('active','price_change') and not r.get('data_stale') and r.get('forecast_eligible', True)), 2)}
    contribution = monthly_contribution if monthly_contribution is not None else baseline['selected_contribution']
    initial = initial_wealth if initial_wealth is not None else wealth['known_net_worth']
    scenarios = []
    if contribution is not None:
        for name, rate in [('Lower growth', annual_return-3), ('Your assumption', annual_return), ('Higher growth', annual_return+3)]:
            scenarios.append({'name': name, **compound_scenario(initial, contribution, rate, inflation, years, target,
                               one_off_amount=one_off_amount, one_off_month=one_off_month)})
    insights = []
    if partial:
        insights.append({'severity': 'warning', 'title': 'Wealth is an observed estimate', 'detail': 'Balances have different statement dates. Missing prices, stale accounts or unverified balances can change the total; review the account dates below.'})
    if hv.get('foreign_cash_unvalued'):
        details = ', '.join(f"{c['currency']} {c['amount']:,.2f}" for c in hv.get('foreign_cash', []) if c.get('value_gbp') is None)
        insights.append({'severity': 'warning', 'title': 'Foreign investment cash is not valued in GBP', 'detail': f'Native cash balances ({details}) are retained, but excluded from known wealth until an exchange-rate valuation is available.'})
    if untracked_fx:
        insights.append({'severity': 'warning', 'title': 'Foreign-currency spending is incomplete', 'detail': 'GBP currency conversions are excluded as money movements; explicitly printed GBP exchange fees remain expenses. Purchases paid from foreign-currency balances are outside this GBP spending ledger, so the contribution baseline can overstate savings.'})
    if not hv.get('position_complete', True):
        insights.append({'severity': 'warning', 'title': 'Investment position history needs review', 'detail': 'Some sell quantities exceed the saved purchase history. Check the connected broker history before relying on the holdings total.'})
    if baseline['months'] < 3:
        insights.append({'severity': 'warning', 'title': 'Limited comparable history', 'detail': f"Only {baseline['months']} fully covered months are available in the last year. Connect or refresh accounts to check available history, or enter your own monthly contribution."})
    else:
        insights.append({'severity': 'info', 'title': 'Use comparable months', 'detail': f"The baseline uses {baseline['months']} complete months from {baseline['period_start']} to {baseline['period_end']}. Current and incompletely covered months are excluded."})
    if contribution is not None and contribution < 0:
        insights.append({'severity': 'warning', 'title': 'Your baseline is drawing down wealth', 'detail': 'Spending exceeds eligible income. Scenarios retain that monthly deficit instead of turning it into assumed savings.'})
    if baseline['exclude_outliers']:
        insights.append({'severity': 'warning', 'title': 'This contribution sets flagged costs aside', 'detail': f"The selected baseline excludes {baseline['excluded_outlier_count']} flagged charges totalling £{baseline['excluded_outlier_spend']:,.2f}. These may be real costs. Recorded mean surplus remains £{baseline['recorded_avg_surplus']:,.2f}. Cash buffers still use recorded spending." if baseline['months'] else 'No fully covered month is available. The outlier filter cannot supply missing statement coverage.'})
    if baseline_method == 'median':
        insights.append({'severity': 'info', 'title': 'Median monthly surplus', 'detail': 'This is the middle of the eligible monthly income-minus-spending results. A large windfall has less influence than in the mean. It is not median income minus median spending, and a typical past month is not a guaranteed future contribution.'})
    if one_off_amount:
        insights.append({'severity': 'info', 'title': 'Planned one-off expense', 'detail': f'£{one_off_amount:,.2f} is withdrawn at the end of month {one_off_month}, after that month’s growth and contribution. The goal date is the first crossing; later withdrawals can bring the balance below the goal again.'})
    if any(r['account_type'] == 'investment' and r['amount'] < 0 for r in cashflow):
        insights.append({'severity': 'warning', 'title': 'Broker card spending uses export coverage', 'detail': 'Broker card purchases remain part of spending. Calendar-month verification covers bank/card statements; investment exports have no independent closing balance and may omit later broker spending.'})
    if quality['failed_statements']:
        insights.append({'severity': 'warning', 'title': 'Statement reconciliation needs attention', 'detail': f"{quality['failed_statements']} statements do not balance. Their months are excluded from the planning baseline."})
    if uncat[0]:
        insights.append({'severity': 'warning', 'title': 'Categorisation needs review', 'detail': f"{uncat[0]} transactions are uncategorised. They still count in cash flow, but category-based insights may be incomplete."})
    insights.append({'severity': 'info', 'title': 'A cash buffer is different from investment wealth', 'detail': 'Runway uses bank cash less all recorded debt, divided by observed monthly spending. It excludes holdings and investment cash. Account inclusion and ownership shares follow your saved planning settings.'})
    return {'as_of': today.isoformat(), 'wealth': wealth, 'baseline': baseline, 'quality': quality,
            'resilience': resilience, 'assumptions': {'years': years, 'annual_return': annual_return,
            'inflation': inflation, 'monthly_contribution': contribution, 'initial_wealth': initial,
            'target': target, 'baseline_method': baseline_method, 'exclude_outliers': exclude_outliers,
            'one_off_amount': one_off_amount, 'one_off_month': one_off_month,
            'contribution_source': 'User input' if monthly_contribution is not None else f"{'Median' if baseline_method == 'median' else 'Mean'} surplus from covered months"},
            'scenarios': scenarios, 'insights': insights,
            'methodology': [
                'Known wealth = latest recorded signed account balances + priced holdings. Credit debt and overdrafts reduce wealth. Missing pensions, property and loans are not estimated.',
                'Income excludes matched transfers, investment trades and recognised refunds. Refunds reduce spending. Investment income and recognised bank interest are removed from the contribution baseline to avoid counting scenario returns twice.',
                'Monthly growth r = (1 + annual return / 100)^(1/12) − 1. Each month: V = V × (1 + r) + contribution. Contributions are fixed nominal amounts paid at month end.',
                'Today’s-money value = future value / (1 + inflation / 100)^years. The goal is a nominal amount; target time is the first modelled month reaching it.',
                'Lower/higher scenarios change the assumed annual return by ±3 percentage points. These are illustrations, not probability bands. The blended return applies to the entered starting wealth; taxes, fees, debt APRs, varying cash yields and asset allocation are not modelled.',
                'Buffer amounts use recorded spending before the optional outlier filter as a proxy, not an inferred essential-expense budget. Recurring charges are already included in spending and are not deducted twice.',
                'A one-off planned expense is a fixed nominal withdrawal at the end of its selected month. Contributed principal is starting wealth plus contributions minus planned expenses. Actual transactions and balances are not changed.',
                'The median contribution is the median of monthly surpluses, not the difference between separate income and spending medians. Outlier-adjusted contributions are optional hypothetical scenarios; excluded costs can be legitimate and refunds are retained.'
            ]}

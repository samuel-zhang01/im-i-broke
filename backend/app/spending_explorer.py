"""Compact, reconcilable category/month/merchant cube for interactive exploration."""
from collections import defaultdict
from datetime import date, timedelta
from . import analytics


def explore(conn, account=None, date_from=None, date_to=None, exclude_outliers=False, outlier_z=3.5):
    selected = analytics._filtered_rows(conn, account, date_from, date_to)
    recorded = [component for row in selected if (component := analytics._household_component(row)) is not None]
    flagged = {r['id'] for r in analytics.detect_anomalies(conn, z=outlier_z, include_resolved=True) if r.get('kind') == 'large_amount'} if exclude_outliers else set()
    cube = defaultdict(lambda: {'gross': 0., 'refunds': 0., 'n': 0})
    weekly_cube = defaultdict(lambda: {'gross': 0., 'refunds': 0., 'n': 0})
    for r in recorded:
        if r['id'] in flagged or (r['amount'] >= 0 and not r['is_refund']):
            continue
        key = (r['txn_date'][:7], r['category'] or 'Uncategorized', r['merchant'] or r['description'] or 'Unknown merchant')
        day = date.fromisoformat(r['txn_date'][:10])
        week = (day - timedelta(days=day.weekday())).isoformat()
        for cell in (cube[key], weekly_cube[(week, key[1], key[2])]):
            cell['n'] += 1
            cell['gross'] += max(0, -r['amount'])
            cell['refunds'] += max(0, r['amount'])
    result = [{'month': month, 'category': category, 'merchant': merchant, 'n': v['n'],
               'gross': round(v['gross'], 2), 'refunds': round(v['refunds'], 2), 'net': round(v['gross']-v['refunds'], 2)}
              for (month, category, merchant), v in sorted(cube.items())]
    # Match the main spending view's observed household period. Excluded
    # investment trades or transfers must not append misleading empty months.
    dates = [r['txn_date'] for r in recorded or selected]
    first, last = date_from or min(dates, default=None), date_to or max(dates, default=None)
    if first and last and ((int(last[:4])-int(first[:4]))*12+int(last[5:7])-int(first[5:7])) > 1200:
        raise ValueError('Select a period of 100 years or less')
    months = [m['m'] for m in analytics._aggregate_months([], first=first[:7], last=last[:7])] if first and last else []
    if len(months) > 1200:
        raise ValueError('Select a period of 100 years or less')
    coverage = analytics.month_coverage(conn, months, account, date_from, date_to)
    weekly_cells = [{'week': week, 'category': category, 'merchant': merchant, 'n': v['n'],
                     'gross': round(v['gross'], 2), 'refunds': round(v['refunds'], 2),
                     'net': round(v['gross'] - v['refunds'], 2)}
                    for (week, category, merchant), v in sorted(weekly_cube.items())]
    weeks, weekly_coverage = [], {}
    if first and last:
        start, end = date.fromisoformat(first[:10]), date.fromisoformat(last[:10])
        cursor = start - timedelta(days=start.weekday())
        while cursor <= end:
            week = cursor.isoformat()
            weeks.append(week)
            days = [date.fromordinal(n) for n in range(cursor.toordinal(), min(cursor.toordinal() + 6, date.max.toordinal()) + 1)]
            weekly_coverage[week] = {
                'coverage_complete': all(start <= day <= end and coverage.get(day.isoformat()[:7], {}).get('coverage_complete', False) for day in days),
            }
            if (end - cursor).days < 7:
                break
            cursor += timedelta(days=7)
    return {'weekly_cells': weekly_cells, 'weeks': weeks, 'weekly_coverage': weekly_coverage, 'cells': result, 'months': months, 'coverage': coverage, 'date_from': first, 'date_to': last}

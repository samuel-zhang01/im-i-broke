"""Pure calendar and amount models for household recurring-payment evidence.

The caller owns classification, account coverage, and freshness. These helpers
never infer that an observed merchant is a subscription or an active bill.
"""
from __future__ import annotations

import calendar
import statistics
from datetime import date, timedelta
from functools import lru_cache


_MONTH_STEPS = {"monthly": 1, "bi-monthly": 2, "quarterly": 3, "annual": 12}
_DAY_STEPS = {"weekly": 7, "fortnightly": 14}
_DEFAULT_TOLERANCE = {"weekly": 2, "fortnightly": 2, "monthly": 5,
                      "bi-monthly": 3, "quarterly": 5, "annual": 15}


def _month_number(value: date) -> int:
    return value.year * 12 + value.month - 1


def _calendar_date(month: int, anchor: int | None) -> date | None:
    year, month0 = divmod(month, 12)
    if not 1 <= year <= 9999:
        return None
    last_day = calendar.monthrange(year, month0 + 1)[1]
    return date(year, month0 + 1, min(anchor or last_day, last_day))


def _fit(dates, cadence, tolerance, required_regularity, minimum):
    months = _MONTH_STEPS.get(cadence)
    days = _DAY_STEPS.get(cadence)
    candidates = []
    if months:
        # A calendar payment on the 29th–31st clips to February's last day.
        # Explicit month-end anchors additionally follow 30-day months.
        for anchor in sorted({value.day for value in dates}) + [None]:
            for phase in sorted({offset % months for offset in range(-1, 2)}):
                origin = _month_number(dates[0]) + phase
                cache = {}
                def due(index, origin=origin, anchor=anchor, cache=cache):
                    if index not in cache:
                        cache[index] = _calendar_date(origin + index * months, anchor)
                    return cache[index]
                candidates.append((anchor, origin, due))
    else:
        # Only the phase modulo one interval matters; repeated observations on
        # a weekday must not create identical candidates for an entire history.
        for offset in sorted({(value - dates[0]).days % days for value in dates}):
            origin = dates[0] + timedelta(days=offset)
            def due(index, origin=origin):
                try:
                    return origin + timedelta(days=index * days)
                except OverflowError:
                    return None
            candidates.append((None, origin, due))

    best = None
    for anchor, origin, due in candidates:
        def distance(value, index):
            expected = due(index)
            return abs((value - expected).days) if expected is not None else float('inf')
        last_phase = ((_month_number(dates[-1]) - origin) / months if months
                      else (dates[-1] - origin).days / days)
        near_last = round(last_phase)
        if min(distance(dates[-1], index) for index in range(near_last - 1, near_last + 2)) > tolerance:
            continue
        supported = []
        occupied = set()
        residuals = []
        for value in dates:
            approximate = ((_month_number(value) - origin) / months if months
                           else (value - origin).days / days)
            near = round(approximate)
            index = min(range(near - 1, near + 2), key=lambda i: distance(value, i))
            expected = due(index)
            if expected is None:
                continue
            residual = (value - expected).days
            if abs(residual) <= tolerance and index not in occupied:
                supported.append((value, index))
                occupied.add(index)
                residuals.append(residual)
        if len(supported) < minimum or supported[-1][0] != dates[-1]:
            continue
        fit = len(supported) / len(dates)
        if fit < required_regularity:
            continue
        increments = [b[1] - a[1] for a, b in zip(supported, supported[1:])]
        direct = sum(increment == 1 for increment in increments)
        missed = sum(max(0, increment - 1) for increment in increments)
        # Multiples alone cannot establish a shorter underlying schedule. One
        # or two missing cycles need at least two observed consecutive pairs.
        if not direct or any(increment <= 0 or increment > 3 for increment in increments):
            continue
        if missed and (direct < 2 or direct / len(increments) < .5):
            continue
        mean_error = sum(abs(value) for value in residuals) / len(residuals)
        score = (fit, -missed, -mean_error, direct, anchor is None)
        if best is not None and score <= best[0]:
            continue
        next_date = due(supported[-1][1] + 1)
        if next_date is None:
            continue
        # The uncertainty window uses observed posting variation; configured
        # tolerance controls fitting, rather than promising unwitnessed drift.
        window = min(tolerance, max(1, max(abs(value) for value in residuals))) if tolerance else 0
        try:
            earliest = next_date - timedelta(days=window)
            latest = next_date + timedelta(days=window)
        except OverflowError:
            continue
        best = (score, {
            "cadence": cadence,
            "cadence_days": round(months * 365.25 / 12 if months else days, 2),
            "regularity": round(fit, 3),
            "next_expected": next_date,
            "next_expected_earliest": earliest,
            "next_expected_latest": latest,
            "forecast_window_days": window,
            "missed_cycles": missed,
            "anchor_day": anchor if months else None,
            "anchor_kind": ("month_end" if anchor is None else "day_of_month") if months else "weekday",
            "normalized_monthly_factor": 1 / months if months else 365.25 / (12 * days),
            "schedule_evidence_count": len(supported),
            "schedule_start": supported[0][0],
            "schedule_dates": tuple(value for value, _ in supported),
        })
    return best


def fit_schedule(dates, *, day_tolerance=None, regularity=.5, min_occurrences=3):
    """Return independent evidence, caching only immutable schedule inputs."""
    result = _cached_schedule(tuple(sorted(set(dates))), tuple(sorted((day_tolerance or {}).items())),
                              regularity, min_occurrences)
    return dict(result) if result is not None else None


@lru_cache(maxsize=512)
def _cached_schedule(dates, day_tolerance, regularity, min_occurrences):
    return _fit_schedule(dates, day_tolerance=dict(day_tolerance), regularity=regularity,
                         min_occurrences=min_occurrences)


def _fit_schedule(dates, *, day_tolerance=None, regularity=.5, min_occurrences=3):
    """Fit named calendar/day schedules; return evidence or ``None``.

    Missing cycles are supported only by observed consecutive cycles. A recent
    schedule can supersede old dates after at least three new observations and
    a substantial improvement in timing fit; all dates remain caller evidence.
    """
    dates = sorted(set(dates))
    minimum = max(2, min_occurrences)
    if len(dates) < minimum:
        return None
    tolerances = {**_DEFAULT_TOLERANCE, **(day_tolerance or {})}
    choices = []
    for cadence in [*_DAY_STEPS, *_MONTH_STEPS]:
        period = _DAY_STEPS.get(cadence) or _MONTH_STEPS[cadence] * 365.25 / 12
        # At least one directly observed interval is necessary before fitting
        # phases. This cheaply rejects dense shopping histories as annual bills.
        gap_slack = tolerances[cadence] * 2 + (3 if cadence in _MONTH_STEPS else 0)
        if not any(abs((right - left).days - period) <= gap_slack for left, right in zip(dates, dates[1:])):
            continue
        required = max(minimum, 3) if cadence == "annual" else minimum
        full = _fit(dates, cadence, tolerances[cadence], regularity, required)
        selected = full
        # Only consider a recent schedule if the full series does not fit well.
        # A two-observation preview must not silently discard earlier evidence.
        if len(dates) > max(3, required) and (full is None or full[1]["regularity"] < .8):
            hiatuses = {i for i in range(1, len(dates)) if (dates[i] - dates[i - 1]).days > period * 3}
            starts = hiatuses | {len(dates) - max(required, size) for size in (3, 6, 12)}
            for start in sorted(i for i in starts if 0 < i <= len(dates) - max(3, required)):
                # Three coincidentally regular latest purchases are not enough
                # to erase a noisy history. Require a preceding coverage break
                # or a previously supported schedule before changing regimes.
                if full is None and start not in hiatuses and not _fit(
                        dates[:start], cadence, tolerances[cadence], .8, max(3, required)):
                    continue
                tail = _fit(dates[start:], cadence, tolerances[cadence], max(.8, regularity), max(3, required))
                if tail and (full is None or tail[1]["regularity"] >= full[1]["regularity"] + .2):
                    selected = tail
                    break
        if selected:
            score, result = selected
            choices.append(((result["regularity"], result["schedule_evidence_count"], *score[1:]), result))
    return max(choices, key=lambda item: item[0])[1] if choices else None


def amount_profile(amounts, *, change_threshold=.30):
    """Estimate a supported recent level and distinguish a repeated price move.

    A median of three resists one latest unusual charge. Two similar latest
    observations can establish a new level only against an earlier baseline.
    """
    values = [abs(float(amount)) for amount in amounts]
    if not values:
        raise ValueError("At least one payment amount is required")
    recent = values[-6:]
    old = values[:-2]
    baseline = statistics.median(old or values)
    latest = values[-2:]
    proposed = statistics.median(latest)
    level_tolerance = min(.05, change_threshold / 2)
    close_latest = len(latest) == 2 and abs(latest[0] - latest[1]) <= max(.01, proposed * level_tolerance)
    regime_start = len(values) - 2 if close_latest else len(values)
    if close_latest:
        while regime_start and abs(values[regime_start - 1] - proposed) <= max(.01, proposed * level_tolerance):
            regime_start -= 1
    earlier = values[:regime_start]
    comparison = statistics.median(earlier[-3:]) if earlier else baseline
    comparison_support = [value for value in earlier[-3:]
                          if abs(value - comparison) <= max(.01, comparison * level_tolerance)]
    baseline_supported = len(comparison_support) >= 2
    departure = bool(comparison and all(abs(value - comparison) > max(.01, comparison * level_tolerance) for value in latest))
    same_direction = all(value > comparison for value in latest) or all(value < comparison for value in latest)
    changed_level = bool(regime_start and close_latest and departure and same_direction)
    if changed_level:
        current = proposed
        # Keep a stable comparison once a new price has been paid repeatedly.
        baseline = comparison if baseline_supported else current
        regime = values[regime_start:]
        basis = ("Two or more recent payments support the changed amount."
                 if baseline_supported else "Two recent payments support this amount; earlier payments do not establish a comparable price.")
    else:
        current = statistics.median(values[-3:])
        regime = recent
        basis = "Median of the latest three payment dates, limiting one unusual charge." if len(values) >= 3 else "Median of the observed payment dates."
    band = max(1, current * .2)
    supported = [value for value in regime if abs(value - current) <= band]
    consistency = len(supported) / len(regime)
    change_percent = 100 * (current - baseline) / baseline if baseline else 0
    supported_change = changed_level and baseline_supported
    confirmed = supported_change and abs(change_percent) > change_threshold * 100
    return {
        "typical_amount": round(baseline, 2),
        "current_amount": round(current, 2),
        "amount_consistency": round(consistency, 3),
        "amount_min": round(min(supported or [current]), 2),
        "amount_max": round(max(supported or [current]), 2),
        "price_change_confirmed": confirmed,
        "price_change_percent": round(change_percent, 2) if supported_change else 0.0,
        "price_change_evidence": len(regime) if supported_change else 0,
        "amount_basis": basis,
    }

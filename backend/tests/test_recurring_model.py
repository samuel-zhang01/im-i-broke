"""Calendar and price regressions motivated by the recurring-payments EDA."""
import unittest
from datetime import date

from app.recurring_model import amount_profile, fit_schedule


def schedule(*dates, **kwargs):
    return fit_schedule([date.fromisoformat(value) for value in dates], **kwargs)


class RecurringModelTests(unittest.TestCase):
    def test_monthly_is_one_payment_per_month_and_keeps_calendar_day(self):
        result = schedule('2026-01-01', '2026-02-01', '2026-03-01', '2026-04-01', '2026-05-01')
        self.assertEqual(result['cadence'], 'monthly')
        self.assertEqual(result['normalized_monthly_factor'], 1)
        self.assertEqual(result['next_expected'], date(2026, 6, 1))

    def test_month_end_and_leap_day_are_calendar_aware(self):
        result = schedule('2024-01-31', '2024-02-29', '2024-03-31')
        self.assertEqual(result['anchor_kind'], 'month_end')
        self.assertEqual(result['next_expected'], date(2024, 4, 30))
        result = schedule('2024-01-30', '2024-02-29', '2024-03-30')
        self.assertEqual(result['anchor_day'], 30)
        self.assertEqual(result['next_expected'], date(2024, 4, 30))

    def test_longer_calendar_cadences_use_exact_monthly_equivalents(self):
        for dates, cadence, factor, expected in [
            (['2026-01-31', '2026-03-31', '2026-05-31'], 'bi-monthly', .5, date(2026, 7, 31)),
            (['2025-10-15', '2026-01-15', '2026-04-15'], 'quarterly', 1/3, date(2026, 7, 15)),
            (['2024-02-29', '2025-02-28', '2026-02-28'], 'annual', 1/12, date(2027, 2, 28)),
        ]:
            with self.subTest(cadence=cadence):
                result = schedule(*dates)
                self.assertEqual(result['cadence'], cadence)
                self.assertEqual(result['normalized_monthly_factor'], factor)
                self.assertEqual(result['next_expected'], expected)

    def test_weekly_uses_weekday_and_named_frequency(self):
        result = schedule('2026-09-01', '2026-09-08', '2026-09-15')
        self.assertEqual(result['cadence'], 'weekly')
        self.assertEqual(result['next_expected'], date(2026, 9, 22))
        self.assertAlmostEqual(result['normalized_monthly_factor'], 365.25/84)

    def test_missing_cycle_needs_consecutive_support(self):
        result = schedule('2026-01-05', '2026-02-05', '2026-04-05', '2026-05-05')
        self.assertEqual(result['cadence'], 'monthly')
        self.assertEqual(result['missed_cycles'], 1)
        self.assertEqual(result['regularity'], 1)
        self.assertIsNone(schedule('2026-01-05', '2026-02-05', '2026-05-05'))

    def test_long_gaps_do_not_create_invented_intervening_cycles(self):
        self.assertIsNone(schedule('2024-01-05', '2026-02-05', '2026-03-05'))
        result = schedule('2024-01-05', '2024-02-05', '2026-07-05', '2026-08-05', '2026-09-05')
        self.assertEqual(result['schedule_start'], date(2026, 7, 5))
        self.assertEqual(result['schedule_evidence_count'], 3)

    def test_preview_two_monthly_dates_does_not_allow_two_annual_dates(self):
        self.assertEqual(schedule('2026-01-05', '2026-02-05', min_occurrences=2)['cadence'], 'monthly')
        self.assertIsNone(schedule('2025-01-05', '2026-01-05', min_occurrences=2))

    def test_regular_calendar_stream_tolerates_extra_merchant_charges(self):
        result = schedule('2026-01-08', '2026-02-08', '2026-03-08', '2026-04-08',
                          '2026-05-08', '2026-06-08', '2026-07-07', '2026-07-27',
                          '2026-08-10', '2026-08-25', '2026-09-08')
        self.assertEqual(result['cadence'], 'monthly')
        self.assertEqual(result['schedule_evidence_count'], 9)
        self.assertEqual(result['next_expected'], date(2026, 10, 8))
        self.assertEqual(result['missed_cycles'], 0)
        self.assertEqual(result['forecast_window_days'], 2)

    def test_supported_dates_exclude_off_schedule_extra_charges(self):
        result = schedule('2026-01-05', '2026-02-05', '2026-03-05',
                          '2026-04-05', '2026-04-15', '2026-04-16', '2026-05-05')
        expected = tuple(date(2026, month, 5) for month in range(1, 6))
        self.assertEqual(result['cadence'], 'monthly')
        self.assertEqual(result['schedule_dates'], expected)
        self.assertEqual(result['schedule_evidence_count'], 5)

    def test_timing_tolerance_and_regularity_stay_authoritative(self):
        dates = ['2026-01-08', '2026-02-08', '2026-03-10', '2026-04-08']
        self.assertIsNone(schedule(*dates, day_tolerance={'monthly': 0}, regularity=1))
        self.assertIsNotNone(schedule(*dates, day_tolerance={'monthly': 2}, regularity=1))

    def test_recent_schedule_shift_needs_three_observations(self):
        result = schedule('2026-01-01', '2026-02-01', '2026-03-01',
                          '2026-04-20', '2026-05-20', '2026-06-20')
        self.assertEqual(result['next_expected'], date(2026, 7, 20))
        self.assertEqual(result['schedule_evidence_count'], 3)

    def test_one_outlier_does_not_invent_a_new_price(self):
        result = amount_profile([10, 10, 10, 10, 30])
        self.assertEqual(result['current_amount'], 10)
        self.assertEqual(result['typical_amount'], 10)
        self.assertFalse(result['price_change_confirmed'])
        self.assertEqual(result['amount_min'], 10)
        self.assertEqual(result['amount_max'], 10)

    def test_repeated_new_price_uses_new_regime_for_consistency(self):
        result = amount_profile([10, 10, 10, 20, 20])
        self.assertEqual(result['current_amount'], 20)
        self.assertEqual(result['typical_amount'], 10)
        self.assertEqual(result['amount_consistency'], 1)
        self.assertTrue(result['price_change_confirmed'])
        self.assertEqual(result['price_change_evidence'], 2)
        self.assertEqual(result['price_change_percent'], 100)

    def test_threshold_controls_alert_but_does_not_hide_repeated_level_evidence(self):
        result = amount_profile([23]*9 + [27.97]*2)
        self.assertEqual(result['current_amount'], 27.97)
        self.assertEqual(result['price_change_evidence'], 2)
        self.assertEqual(result['price_change_percent'], 21.61)
        self.assertFalse(result['price_change_confirmed'])
        self.assertTrue(amount_profile([23]*9 + [27.97]*2, change_threshold=.2)['price_change_confirmed'])

    def test_sensitive_price_threshold_does_not_merge_the_old_and_new_levels(self):
        result = amount_profile([10, 10, 10.2, 10.2], change_threshold=.01)
        self.assertTrue(result['price_change_confirmed'])
        self.assertEqual(result['price_change_percent'], 2)

    def test_live_eda_amount_shapes_have_robust_recent_estimates(self):
        self.assertEqual(amount_profile([49, 49, 49, 59, 49])['current_amount'], 49)
        result = amount_profile([2.99, 2.99, 29, 2.99, 19.99, 2.99])
        self.assertEqual(result['current_amount'], 2.99)
        self.assertGreaterEqual(result['amount_consistency'], 2/3)
        self.assertEqual((result['amount_min'], result['amount_max']), (2.99, 2.99))

    def test_price_comparison_excludes_entire_latest_supported_regime(self):
        result = amount_profile([10, 10, 20, 20, 20, 20, 20])
        self.assertEqual(result['typical_amount'], 10)
        self.assertEqual(result['price_change_evidence'], 5)

    def test_price_comparison_needs_repeated_earlier_level(self):
        for amounts, expected in [([20] + [10]*20, 10), ([264.96, 25.01, 21.1, 21.32], 21.21)]:
            with self.subTest(amounts=amounts):
                result = amount_profile(amounts)
                self.assertEqual(result['current_amount'], expected)
                self.assertEqual(result['typical_amount'], expected)
                self.assertFalse(result['price_change_confirmed'])
                self.assertEqual(result['price_change_percent'], 0)
                self.assertEqual(result['price_change_evidence'], 0)

    def test_price_baseline_uses_recent_supported_level(self):
        result = amount_profile([10]*10 + [15]*3 + [20]*2)
        self.assertEqual(result['typical_amount'], 15)
        self.assertEqual(result['price_change_percent'], 33.33)
        self.assertTrue(result['price_change_confirmed'])

    def test_cached_schedule_is_independent_and_new_evidence_changes_result(self):
        dates = ['2026-01-05', '2026-02-05', '2026-03-05']
        first = schedule(*dates)
        first['next_expected'] = date(1900, 1, 1)
        self.assertEqual(schedule(*dates)['next_expected'], date(2026, 4, 5))
        self.assertEqual(schedule(*dates, '2026-04-05')['next_expected'], date(2026, 5, 5))

    def test_supported_date_boundaries_do_not_overflow_candidate_or_forecast_dates(self):
        result = schedule('0001-01-05', '0001-02-05', '0001-03-05')
        self.assertEqual(result['next_expected'], date(1, 4, 5))
        self.assertIsNone(schedule('9999-10-05', '9999-11-05', '9999-12-05'))
        self.assertIsNone(schedule('9999-12-10', '9999-12-17', '9999-12-24'))


if __name__ == '__main__':
    unittest.main()

"""Calendar coverage and NAV movement, with no invented profit or return."""
from datetime import date, timedelta
import sqlite3
import unittest

from app import investing_calendar as calendar


TODAY = date(2026, 9, 18)


def account(id='one', paper=False):
    return {'id': id, 'is_paper': paper, 'history': {'data': []},
            'activities': {'status': 'ok', 'truncated': False, 'data': [
                {'date': '2026-09-02', 'type': 'DEPOSIT', 'amount': 500, 'currency': 'GBP'}]}}


def point(day, value, *, id='one', code='GBP'):
    return {'account_id': id, 'date': day, 'currency': code, 'total_value': value,
            'retrieved_at': '2026-09-18T12:00:00Z'}


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.execute('CREATE TABLE reference_fx(date TEXT,currency TEXT,rate_to_gbp REAL,observed_date TEXT)')
        self.cached = {'accounts': [account()], 'retrieved_at': '2026-09-18T12:00:00Z'}
        self.estimates, self.observations = [], []

    def data(self, **kwargs):
        return calendar.bundle(self.conn, today=TODAY, year=2026, month=9, cached=self.cached,
                               estimates=self.estimates, observations=self.observations, **kwargs)

    def test_deposits_are_not_profit_and_only_consecutive_days_get_change(self):
        self.estimates = [point('2026-09-01', 100), point('2026-09-02', 600), point('2026-09-04', 610)]
        result = self.data()
        days = result['days']
        self.assertIsNone(days[0]['change'])
        self.assertEqual(days[1]['change'], 500)
        self.assertEqual(days[1]['change_pct'], 500)
        self.assertIsNone(days[1]['profit'])
        self.assertIsNone(days[1]['external_flows'])
        self.assertIsNone(days[1]['return_pct'])
        self.assertIsNone(days[2]['end_value'])
        self.assertIsNone(days[3]['change'])
        self.assertEqual(days[3]['reason'], 'missing_previous_day')
        self.assertEqual(days[3]['observation_change'], 10)
        self.assertEqual(days[3]['observation_start_date'], '2026-09-02')
        self.assertEqual(days[3]['observation_days'], 2)
        self.assertIsNone(days[2]['observation_change'])
        self.assertFalse(result['summary']['complete'])
        self.assertIsNone(result['summary']['change'])
        self.assertEqual(result['summary']['positive_days'], 1)

    def test_month_to_date_requires_endpoints_but_daily_counts_keep_gaps(self):
        self.estimates = [point((date(2026, 8, 31) + timedelta(days=i)).isoformat(), 100 + i)
                          for i in range(19)]
        result = self.data()
        self.assertEqual(result['summary']['change'], 18)
        self.assertEqual(result['summary']['observed_days'], 18)
        self.assertEqual(result['summary']['expected_days'], 18)
        self.assertTrue(result['summary']['complete'])
        self.assertEqual(result['summary']['positive_days'], 18)
        self.assertEqual(result['days'][18]['reason'], 'future')
        self.assertIsNone(result['days'][18]['change'])
        self.estimates.pop(7)
        incomplete = self.data()
        self.assertTrue(incomplete['summary']['complete'])
        self.assertFalse(incomplete['summary']['daily_coverage_complete'])
        self.assertEqual(incomplete['summary']['change'], 18)
        self.assertEqual(incomplete['summary']['observed_days'], 16)

    def test_sources_never_blend_or_substitute_for_a_missing_day(self):
        self.estimates = [point('2026-09-01', 100), point('2026-09-02', 105)]
        self.observations = [{**point('2026-09-02', 999), 'observed_at': '2026-09-02T14:00:00Z'},
                             {**point('2026-09-03', 1000), 'observed_at': '2026-09-03T14:00:00Z'}]
        estimated, observed = self.data(), self.data(source='recorded')
        self.assertEqual(estimated['days'][1]['change'], 5)
        self.assertIsNone(estimated['days'][2]['change'])
        self.assertEqual(observed['days'][1]['end_value'], 999)
        self.assertIsNone(observed['days'][1]['change'])
        self.assertEqual(observed['days'][2]['change'], 1)
        self.assertFalse(observed['days'][2]['estimated'])

    def test_new_account_coverage_does_not_become_a_daily_gain(self):
        self.cached['accounts'].append(account('two'))
        self.estimates = [point('2026-09-01', 100), point('2026-09-02', 105),
                          point('2026-09-02', 900, id='two')]
        result = self.data()
        self.assertIsNone(result['days'][0]['end_value'])
        self.assertEqual(result['days'][0]['known_accounts'], 1)
        self.assertEqual(result['days'][1]['end_value'], 1005)
        self.assertIsNone(result['days'][1]['change'])
        self.assertEqual(self.data(account_ids=['one'])['days'][1]['change'], 5)

    def test_currency_conversion_uses_each_date_and_no_future_fx(self):
        self.estimates = [point('2026-09-01', 100, code='USD'), point('2026-09-02', 100, code='USD')]
        self.conn.executemany('INSERT INTO reference_fx VALUES(?,?,?,?)', [
            ('2026-09-01', 'USD', .75, '2026-09-01'), ('2026-09-02', 'USD', .8, '2026-09-02')])
        result = self.data()
        self.assertEqual(result['days'][1]['change'], 5)
        self.conn.execute("DELETE FROM reference_fx WHERE date='2026-09-01'")
        result = self.data()
        self.assertIsNone(result['days'][1]['change'])
        self.assertEqual(self.data(currency_code='USD')['days'][1]['change'], 0)

    def test_latest_observation_wins_and_ambiguous_estimates_remain_unknown(self):
        self.observations = [
            {**point('2026-09-01', 100), 'observed_at': '2026-09-01T10:00:00Z'},
            {**point('2026-09-01', 110), 'observed_at': '2026-09-01T15:00:00Z'},
            {**point('2026-09-02', 120), 'observed_at': '2026-09-02T15:00:00Z'},
        ]
        self.assertEqual(self.data(source='recorded')['days'][1]['change'], 10)
        self.estimates = [point('2026-09-01', 100), point('2026-09-01', 110), point('2026-09-02', 120)]
        self.assertIsNone(self.data()['days'][1]['change'])

    def test_paper_and_empty_selection_remain_separate(self):
        self.cached['accounts'].append(account('paper', True))
        self.estimates = [point('2026-09-01', 100), point('2026-09-02', 105),
                          point('2026-09-01', 100000, id='paper'), point('2026-09-02', 200000, id='paper')]
        self.assertEqual(self.data()['days'][1]['change'], 5)
        self.assertEqual(self.data(paper=True)['days'][1]['change'], 100000)
        self.assertTrue(self.data(account_ids=['paper'])['paper'])
        with self.assertRaises(ValueError):
            self.data(account_ids=['one', 'paper'])
        self.assertEqual(self.data(account_ids=['__none__'])['days'][0]['reason'], 'no_accounts')

    def test_invalid_parameters_and_bad_points_are_not_silently_accepted(self):
        for kwargs in ({'source': 'profit'}, {'currency_code': 'GBp'}, {'account_ids': ['missing']}):
            with self.assertRaises(ValueError):
                self.data(**kwargs)
        for kwargs in ({'year': 1969}, {'month': 13}):
            with self.assertRaises(ValueError):
                calendar.bundle(self.conn, today=TODAY, **kwargs)
        self.estimates = [point('2026-09-01', float('nan')), point('2026-09-02', 100),
                          point('2027-01-01', 900), point('bad date', 900)]
        self.assertIsNone(self.data()['days'][1]['change'])

    def test_saved_history_fallback_and_zero_are_distinct_from_unknown(self):
        self.cached['accounts'][0]['history']['data'] = [
            {'date': '2026-09-01', 'total_value': 0, 'currency': 'GBP'},
            {'date': '2026-09-02', 'total_value': 0, 'currency': 'GBP'}]
        result = self.data()
        self.assertEqual(result['days'][1]['change'], 0)
        self.assertIsNone(result['days'][1]['change_pct'])
        self.assertTrue(result['days'][1]['complete'])
        self.assertEqual(result['summary']['flat_days'], 1)


if __name__ == '__main__':
    unittest.main()

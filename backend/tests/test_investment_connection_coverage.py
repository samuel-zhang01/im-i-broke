"""Coverage distinguishes unknown, empty, retained and source time; no network."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import sqlite3
import unittest
from unittest.mock import patch

from app import investment_coverage


class InvestmentConnectionCoverageTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.execute('PRAGMA query_only=ON')
        self.account = {'id': 'live', 'name': 'Example', 'institution': 'Demo', 'connection_id': 'link',
                        'is_paper': False, 'total_value': 0, 'initial_sync_complete': None,
                        'holdings_synced_at': '2026-09-16T12:00:00Z', 'activities_synced_at': None,
                        'positions': {'status': 'empty', 'data': [], 'cached_at': '2026-09-18T12:00:00Z'},
                        'balances': {'status': 'ok', 'data': [{'currency': 'GBP', 'cash': 0}]},
                        'activities': {'status': 'unsupported', 'data': []},
                        'history': {'status': 'ok', 'data': [{'date': '2026-09-14'}, {'date': '2026-09-16'}]},
                        'return_rates': {'status': 'unsupported', 'data': []}}
        self.cached = {'accounts': [self.account], 'connections': {'data': [{'id': 'link', 'disabled': False}]},
                       'retrieved_at': '2026-09-18T12:00:00Z'}

    def result(self, **kwargs):
        return investment_coverage.bundle(self.conn, cached=self.cached,
            now=datetime(2026, 9, 18, 13, tzinfo=timezone.utc), **kwargs)

    def test_zero_total_is_known_unsupported_is_not_empty_and_source_time_is_distinct(self):
        result = self.result(); account = result['accounts'][0]
        self.assertTrue(account['account_value_known'])
        sections = {section['key']: section for section in account['sections']}
        self.assertEqual(sections['positions']['status'], 'empty')
        self.assertEqual(sections['activities']['status'], 'unsupported')
        self.assertEqual(sections['positions']['source_at'], '2026-09-16T12:00:00Z')
        self.assertEqual(sections['positions']['retrieved_at'], '2026-09-18T12:00:00Z')
        self.assertEqual(sections['positions']['source_age_days'], 2)
        self.assertIsNone(sections['activities']['source_age_days'])
        self.assertEqual(sections['history']['first_date'], '2026-09-14')
        self.assertIsNone(sections['history']['source_at'])
        self.account['total_value'] = None
        self.assertFalse(self.result()['accounts'][0]['account_value_known'])
        self.assertFalse(result['performance']['personal_return_available'])
        json.dumps(result, allow_nan=False)

    def test_retained_rows_do_not_inherit_new_metadata_time(self):
        self.account['positions'].update(stale=True, truncated=True, status='error', cached_at=None)
        result = self.result()['accounts'][0]['sections'][1]
        self.assertTrue(result['retained'])
        self.assertTrue(result['truncated'])
        self.assertIsNone(result['source_at'])
        self.assertIsNone(result['retrieved_at'])
        self.account['positions'].update(as_of='2026-09-10T12:00:00Z', cached_at='2026-09-11T12:00:00Z')
        result = self.result()['accounts'][0]['sections'][1]
        self.assertEqual(result['source_at'], '2026-09-10T12:00:00Z')
        self.assertTrue(result['source_outdated'])

    def test_selections_empty_invalid_and_paper_separation(self):
        paper = deepcopy(self.account); paper.update(id='paper', is_paper=True)
        self.cached['accounts'].append(paper)
        self.assertEqual(self.result()['selected_account_ids'], ['live'])
        self.assertEqual(self.result(paper=True)['selected_account_ids'], ['paper'])
        self.assertEqual(self.result(account_ids=['__none__'])['accounts'], [])
        self.assertEqual(self.result(account_ids=['live', 'live'])['selected_account_ids'], ['live'])
        with self.assertRaises(ValueError): self.result(account_ids=['live', 'paper'])
        with self.assertRaises(ValueError): self.result(account_ids=['missing'])

    def test_read_does_not_create_archive_tables_or_call_provider(self):
        with patch('app.snaptrade_client.fetch_portfolio', side_effect=AssertionError('Unexpected provider call')):
            result = self.result()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0], 0)
        self.assertEqual(result['accounts'][0]['activity_archive']['stored_activities'], 0)
        self.assertFalse(result['accounts'][0]['activity_archive']['history_complete'])

    def test_disabled_connection_and_future_source_dates_are_visible(self):
        self.cached['connections']['data'][0]['disabled'] = True
        self.account['holdings_synced_at'] = '2026-09-20'
        result = self.result()['accounts'][0]
        self.assertTrue(result['connection_disabled'])
        self.assertTrue(result['sections'][1]['source_outdated'])
        self.assertEqual(result['sections'][1]['source_age_days'], -2)


if __name__ == '__main__':
    unittest.main()

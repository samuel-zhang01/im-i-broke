"""Durable normalized activity and honest fetch coverage; synthetic DBs only."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from app import investment_activity as archive, snaptrade_models as models, snaptrade_store as store
from app.snaptrade_client import fetch_section, RequestBudgetExceeded


def portfolio(at='2026-09-18T12:00:00+00:00', rows=None, *, status='ok', truncated=False,
              window=True, paper=False, account_id='synthetic-account'):
    account = models.account({'id': account_id, 'name': 'Synthetic account',
                              'institution_name': 'Example broker', 'is_paper': paper})
    activity = models.section(rows or [], error='Temporarily unavailable' if status == 'error' else None,
                              truncated=truncated)
    if window:
        activity.update(requested_start='2026-06-20', requested_end='2026-09-18',
                        pages_fetched=1, pagination_complete=status != 'error' and not truncated)
    account['activities'] = activity
    return {'configured': True, 'retrieved_at': at, 'error': None,
            'accounts': [account], 'connections': models.section()}


def activity(id='event-one', amount=10, when='2026-08-01T10:00:00Z'):
    return models.activity({'id': id, 'type': 'DIVIDEND', 'amount': amount,
                            'currency': 'GBP', 'trade_date': when,
                            'description': 'Synthetic dividend', 'units': 2})


class InvestmentActivityTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'finance.db'

    def save(self, *args, **kwargs):
        return store.save_portfolio(portfolio(*args, **kwargs), self.path)

    def read(self, **kwargs):
        return archive.saved_activities('synthetic-account', path=self.path, **kwargs)

    def test_first_duplicate_and_corrected_events_preserve_identity_and_original_observation(self):
        self.save(rows=[activity()])
        self.save(rows=[activity()])
        first = self.read()
        self.assertEqual(first['pagination']['total'], 1)
        self.assertEqual(first['activities'][0]['amount'], 10)
        self.assertEqual(first['coverage']['fully_fetched_request_count'], 1)
        self.assertEqual(first['coverage']['last_fully_fetched_window']['start'], '2026-06-20')
        self.assertFalse(first['coverage']['history_complete'])
        self.save('2026-09-19T12:00:00+00:00', rows=[activity(amount=12)])
        result = self.read()
        self.assertEqual(result['pagination']['total'], 1)
        self.assertEqual(result['activities'][0]['amount'], 12)
        self.assertEqual(result['activities'][0]['first_seen_at'], '2026-09-18T12:00:00+00:00')
        self.assertEqual(result['activities'][0]['last_seen_at'], '2026-09-19T12:00:00+00:00')

    def test_rolling_window_and_empty_fetch_never_remove_saved_activities(self):
        self.save(rows=[activity(when='2026-06-21T10:00:00Z')])
        self.save('2026-12-18T12:00:00+00:00', rows=[activity('event-two', when='2026-12-17T10:00:00Z')])
        self.save('2026-12-19T12:00:00+00:00', rows=[])
        result = self.read()
        self.assertEqual(result['pagination']['total'], 2)
        self.assertEqual(result['coverage']['fully_fetched_request_count'], 3)
        self.assertEqual(result['coverage']['first_activity_at'], '2026-06-21T10:00:00Z')
        self.assertEqual(result['coverage']['last_request_status'], 'empty')

    def test_partial_failed_and_truncated_fetches_archive_only_fresh_rows_and_never_claim_window(self):
        self.save(rows=[activity()])
        self.save('2026-09-19T12:00:00+00:00', rows=[activity('event-two')], status='error')
        self.save('2026-09-20T12:00:00+00:00', rows=[], status='error')
        self.save('2026-09-21T12:00:00+00:00', rows=[activity('event-three')], truncated=True)
        result = self.read()
        self.assertEqual(result['pagination']['total'], 3)
        one = next(row for row in result['activities'] if row['id'] == 'event-one')
        self.assertEqual(one['last_seen_at'], '2026-09-18T12:00:00+00:00')
        self.assertEqual(result['coverage']['fully_fetched_request_count'], 1)
        self.assertEqual(result['coverage']['partial_or_failed_requests'], 3)
        self.assertTrue(result['coverage']['last_request_truncated'])

    def test_generated_ids_are_not_cross_refresh_identity(self):
        row = models.activity({'type': 'DIVIDEND', 'amount': 1})
        self.save(rows=[row])
        self.assertEqual(self.read()['pagination']['total'], 0)
        coverage = self.read()['coverage']
        self.assertEqual(coverage['skipped_activity_rows'], 1)
        self.assertEqual(coverage['fully_fetched_request_count'], 0)

    def test_seed_is_idempotent_preserves_existing_fields_and_claims_no_request_window(self):
        raw = portfolio(rows=[activity(), models.activity({'type': 'BUY'}, 1)])
        raw['accounts'][0]['activities']['data'][0].pop('id_source')
        raw['accounts'][0]['activities']['data'][0]['raw_payload'] = 'PRIVATE'
        with store.database(self.path) as conn:
            conn.execute('INSERT INTO snaptrade_cache VALUES(1,?,?,?,?,NULL)',
                         (json.dumps(raw), raw['retrieved_at'], raw['retrieved_at'], raw['retrieved_at']))
            conn.commit()
        self.assertEqual(self.read()['pagination']['total'], 0)  # reading never seeds
        store.seed_activity_archive(self.path)
        store.seed_activity_archive(self.path)
        result = self.read()
        self.assertEqual(result['pagination']['total'], 1)
        self.assertEqual(result['coverage']['cache_seed_count'], 1)
        self.assertEqual(result['coverage']['fully_fetched_windows'], [])
        self.assertIsNone(result['coverage']['last_attempt_at'])
        self.assertIsNone(result['coverage']['last_requested_start'])
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_account_scope_paper_mode_and_disconnected_metadata_are_retained(self):
        self.save(rows=[activity()], paper=False)
        self.save('2026-09-19T12:00:00+00:00', rows=[activity()], paper=True, account_id='paper-account')
        accounts = archive.list_activity_accounts(path=self.path)
        self.assertEqual({row['id']: (row['is_paper'], row['stored_activities']) for row in accounts},
                         {'synthetic-account': (False, 1), 'paper-account': (True, 1)})
        self.assertEqual(self.read()['account']['name'], 'Synthetic account')
        coverage = archive.list_activity_coverage(path=self.path, account_ids=['paper-account'])
        self.assertEqual(set(coverage), {'paper-account'})
        self.assertIs(coverage['paper-account']['is_paper'], True)
        self.assertEqual(archive.list_activity_coverage(path=self.path, account_ids=[]), {})

    def test_pagination_is_bounded_stable_and_read_only(self):
        self.save(rows=[activity(f'event-{index}') for index in range(4)])
        first, second = self.read(limit=2), self.read(limit=2, offset=2)
        self.assertTrue(first['pagination']['has_more'])
        self.assertFalse(second['pagination']['has_more'])
        self.assertEqual(len({row['id'] for row in first['activities'] + second['activities']}), 4)
        for kwargs in ({'limit': 0}, {'limit': 201}, {'offset': -1}):
            with self.assertRaises(ValueError): self.read(**kwargs)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            before = conn.execute('SELECT COUNT(*) FROM snaptrade_activity_fetches').fetchone()[0]
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as conn, conn:
            conn.row_factory = sqlite3.Row
            self.assertEqual(archive.list_activity_coverage(conn)['synthetic-account']['stored_activities'], 4)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM snaptrade_activity_fetches').fetchone()[0], before)

    def test_coverage_windows_are_capped(self):
        at = datetime(2026, 9, 18, tzinfo=timezone.utc)
        for index in range(25):
            self.save((at + timedelta(hours=index)).isoformat(), rows=[])
        coverage = self.read()['coverage']
        self.assertEqual(len(coverage['fully_fetched_windows']), 20)
        self.assertTrue(coverage['coverage_windows_truncated'])
        self.assertEqual(coverage['fully_fetched_request_count'], 25)


class ActivityFetchCoverageTests(unittest.TestCase):
    def test_successful_empty_response_records_actual_requested_window(self):
        read = Mock(return_value={'data': [], 'pagination': {'total': 0}})
        section = fetch_section('activities', 'synthetic', read)
        self.assertEqual(section['requested_start'], read.call_args.kwargs['start_date'].isoformat())
        self.assertEqual(section['requested_end'], read.call_args.kwargs['end_date'].isoformat())
        self.assertEqual(section['pages_fetched'], 1)
        self.assertTrue(section['pagination_complete'])

    def test_failed_page_keeps_window_but_not_exhaustion(self):
        read = Mock(side_effect=[{'data': [{'id': 'one'}], 'pagination': {'total': 300}}, RuntimeError('PRIVATE')])
        section = fetch_section('activities', 'synthetic', read)
        self.assertEqual(section['pages_fetched'], 1)
        self.assertFalse(section['pagination_complete'])
        self.assertNotIn('PRIVATE', json.dumps(section))

    def test_exhausted_budget_before_first_request_does_not_invent_a_window(self):
        section = fetch_section('activities', 'synthetic', Mock(side_effect=RequestBudgetExceeded()))
        self.assertNotIn('requested_start', section)
        self.assertNotIn('requested_end', section)
        self.assertEqual(section['status'], 'error')

    def test_short_or_overlapping_pages_do_not_certify_reported_total(self):
        for body in ({'data': [], 'pagination': {'total': 20}},
                     {'data': [{'id': 'one'}, {'id': 'one'}], 'pagination': {'total': 2}}):
            section = fetch_section('activities', 'synthetic', Mock(return_value=body))
            self.assertFalse(section['pagination_complete'])
            self.assertTrue(section['truncated'])

    def test_invalid_pagination_retains_valid_rows_without_coverage_claim(self):
        section = fetch_section('activities', 'synthetic', Mock(return_value={
            'data': [{'id': 'one'}], 'pagination': {'total': 'NaN'}}))
        self.assertEqual(section['status'], 'error')
        self.assertEqual(section['data'][0]['id'], 'one')
        self.assertFalse(section['pagination_complete'])

    def test_provider_ignoring_page_limit_stays_bounded_and_incomplete(self):
        read = Mock(return_value={'data': [{'id': str(index)} for index in range(150)],
                                  'pagination': {'total': 150}})
        section = fetch_section('activities', 'synthetic', read)
        self.assertEqual(len(section['data']), 100)
        self.assertEqual(read.call_count, 1)
        self.assertTrue(section['truncated'])
        self.assertFalse(section['pagination_complete'])

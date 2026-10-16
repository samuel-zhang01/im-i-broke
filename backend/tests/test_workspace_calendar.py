"""Workspace date policy at real UTC boundaries, using disposable evidence."""
from datetime import date, datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import account_coverage, account_settings, bank_balance_rules, db, lunchflow_store
from app import planning, wealth, wealth_routes, workspace
from app.investment_read_cache import cache


class WorkspaceCalendarTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(folder) / 'synthetic.db'
        self.enterContext(patch.object(db, 'DB_PATH', self.path))
        self.conn = db.init_db(self.path)
        self.addCleanup(self.conn.close)
        app = FastAPI()
        for router in (workspace.router, account_settings.router, account_coverage.router, wealth_routes.router):
            app.include_router(router)
        self.client = self.enterContext(TestClient(app))
        cache.clear()
        self.addCleanup(cache.clear)
        with self.conn:
            self.account = account_settings.create_manual(self.conn, account_settings.ManualAccount(
                display_name='Synthetic wallet', institution='Example Bank'))['id']

    def clock(self, instant):
        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)
        for module in (workspace, wealth, lunchflow_store):
            self.enterContext(patch.object(module, 'datetime', FrozenDateTime))

    def choose_timezone(self, zone):
        response = self.client.patch('/api/workspace/preferences', json={'timezone': zone})
        self.assertEqual(response.status_code, 200, response.text)

    def record_balance(self, day, amount=100):
        return self.client.post(f'/api/workspace/accounts/{self.account}/balances',
                                json={'amount': amount, 'currency': 'GBP', 'as_of': day})

    def assert_visible(self, day, amount):
        snapshot = planning.account_snapshot(self.conn)
        self.assertEqual((snapshot[0]['balance'], snapshot[0]['balance_asof']), (amount, day))
        self.assertEqual(planning.planning_bundle(self.conn)['as_of'], day)
        response = self.client.get('/api/wealth?range=1m')
        self.assertEqual(response.status_code, 200, response.text)
        report = response.json()
        self.assertEqual(report['summary']['net_worth'], amount)
        self.assertEqual(report['points'][-1]['date'], day)
        self.assertEqual(report['points'][-1]['net_worth'], amount)
        self.assertEqual(report['accounts'][0]['as_of'], day)

    def test_tokyo_today_is_accepted_and_visible_before_utc_midnight(self):
        self.clock(datetime(2026, 10, 4, 23, 30, tzinfo=timezone.utc))
        self.choose_timezone('Asia/Tokyo')
        self.assertEqual(self.client.get('/api/workspace/calendar').json(),
                         {'today': '2026-10-05', 'timezone': 'Asia/Tokyo'})
        self.assertEqual(self.record_balance('2026-10-05').status_code, 201)
        self.assertEqual(self.record_balance('2026-10-06', 999).status_code, 409)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM manual_balance_observations').fetchone()[0], 1)
        self.assert_visible('2026-10-05', 100)

    def test_los_angeles_rejects_next_day_after_utc_midnight(self):
        self.clock(datetime(2026, 10, 5, 0, 30, tzinfo=timezone.utc))
        self.choose_timezone('America/Los_Angeles')
        self.assertEqual(self.client.get('/api/workspace/calendar').json(),
                         {'today': '2026-10-04', 'timezone': 'America/Los_Angeles'})
        self.assertEqual(self.record_balance('2026-10-04').status_code, 201)
        self.assertEqual(self.record_balance('2026-10-05', 999).status_code, 409)
        self.assert_visible('2026-10-04', 100)

    def test_confirmed_coverage_uses_same_calendar_as_manual_balance(self):
        self.clock(datetime(2026, 10, 5, 0, 30, tzinfo=timezone.utc))
        for zone, today, tomorrow in (
                ('America/Los_Angeles', '2026-10-04', '2026-10-05'),
                ('Asia/Tokyo', '2026-10-05', '2026-10-06')):
            with self.subTest(zone=zone):
                self.choose_timezone(zone)
                body = {'period_start': '2026-10-01', 'period_end': today,
                        'complete': True, 'evidence': 'Synthetic complete export'}
                url = f'/api/workspace/accounts/{self.account}/coverage'
                self.assertEqual(self.client.post(url, json=body).status_code, 201)
                self.assertEqual(self.client.post(url, json=body | {'period_end': tomorrow}).status_code, 422)

    def test_current_provider_receipt_remains_visible_on_previous_workspace_day(self):
        stamp = '2026-10-05T00:30:00+00:00'
        self.clock(datetime.fromisoformat(stamp))
        self.choose_timezone('America/Los_Angeles')
        with self.conn:
            self.conn.execute('''INSERT INTO bank_connections(id,account_id,name,institution_name,currency,
                status,raw,first_seen_at,last_seen_at) VALUES('feed',?,'Synthetic feed','Example Bank',
                'GBP','ACTIVE','{}',?,?)''', (self.account, stamp, stamp))
            self.conn.execute('''INSERT INTO bank_balance_observations(connection_id,amount,currency,fetched_at)
                VALUES('feed',125,'GBP',?)''', (stamp,))
        response = self.client.get('/api/wealth?range=1m')
        self.assertEqual(response.status_code, 200, response.text)
        report = response.json()
        self.assertEqual(report['summary']['net_worth'], 125)
        self.assertEqual(report['accounts'][0]['as_of'], '2026-10-05')
        self.assertEqual(report['accounts'][0]['fetched_at'], stamp)
        self.assertEqual([(point['date'], point['net_worth']) for point in report['points']], [('2026-10-04', 125)])
        self.assertEqual(planning.account_snapshot(self.conn)[0]['balance'], 125)
        # Explicit historical requests still use their specified day as both
        # cutoffs; live source-clock handling must not introduce wall-clock data.
        historical = wealth.bundle(self.conn, path=self.path, today=date(2026, 10, 4))
        self.assertIsNone(historical['summary']['net_worth'])

    def test_calendar_read_keeps_preferences_patch_shape(self):
        self.clock(datetime(2026, 10, 4, 23, 30, tzinfo=timezone.utc))
        self.choose_timezone('Asia/Tokyo')
        preferences = self.client.get('/api/workspace/preferences').json()
        self.assertEqual(set(preferences), {'reporting_currency', 'locale', 'timezone'})
        self.assertEqual(self.client.patch('/api/workspace/preferences', json=preferences).status_code, 200)

    def test_wealth_cache_changes_at_workspace_midnight_without_a_commit(self):
        self.clock(datetime(2026, 10, 4, 14, 59, tzinfo=timezone.utc))
        self.choose_timezone('Asia/Tokyo')
        self.assertEqual(self.record_balance('2026-10-04').status_code, 201)
        # This minimal-router fixture has no application startup lifespan;
        # initialize the optional broker read schema before measuring caching.
        wealth.sources(self.conn, path=self.path)
        first = self.client.get('/api/wealth?range=1m')
        self.assertEqual(first.json()['points'][-1]['date'], '2026-10-04')
        self.assertEqual(self.client.get('/api/wealth?range=1m').headers['x-ledger-investment-cache'], 'HIT')
        self.clock(datetime(2026, 10, 4, 15, 1, tzinfo=timezone.utc))
        response = self.client.get('/api/wealth?range=1m')
        self.assertEqual(response.headers['x-ledger-investment-cache'], 'MISS')
        self.assertEqual(response.json()['points'][-1]['date'], '2026-10-05')
        self.assertEqual(response.json()['summary']['net_worth'], 100)

    def test_card_debt_keeps_saved_utc_source_date_across_workspace_midnight(self):
        self.choose_timezone('America/Los_Angeles')
        self.clock(datetime(2026, 10, 4, 23, 30, tzinfo=timezone.utc))
        with self.conn:
            account_settings.save_settings(self.conn, self.account, {'account_type': 'credit'})
            self.conn.execute('''INSERT INTO bank_connections(id,account_id,name,institution_name,currency,
                status,raw,first_seen_at,last_seen_at) VALUES('card',?,'Synthetic card','Example Bank',
                'GBP','ACTIVE','{}','2026-10-04T23:00:00+00:00','2026-10-04T23:00:00+00:00')''', (self.account,))
            with patch.object(bank_balance_rules, 'datetime', workspace.datetime):
                bank_balance_rules.set_rule(self.conn, 'card', balance_type='available_credit',
                                            credit_limit=1000, currency='GBP')
        stamp = '2026-10-05T00:30:00+00:00'
        self.clock(datetime.fromisoformat(stamp))
        with self.conn:
            lunchflow_store.record_account_result(self.conn, 'card', {
                'balance': {'amount': 800, 'currency': 'GBP'}, 'transactions': [], 'errors': []}, stamp)
        response = self.client.get('/api/wealth?range=1m')
        self.assertEqual(response.status_code, 200, response.text)
        report = response.json()
        self.assertEqual(report['summary']['net_worth'], -200)
        self.assertEqual(report['points'][-1]['date'], '2026-10-04')
        self.assertEqual(report['points'][-1]['net_worth'], -200)
        saved = self.conn.execute('SELECT effective_amount,fetched_at FROM bank_credit_valuations').fetchone()
        self.assertEqual(tuple(saved), (-200, stamp))
        self.assertEqual(report['accounts'][0]['credit_valuation_coverage']['first_date'], '2026-10-05')


if __name__ == '__main__':
    unittest.main()

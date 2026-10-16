"""Wealth arithmetic with synthetic accounts only; never calls a provider."""
from datetime import date
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app import wealth, source_reconciliation


TODAY = date(2026, 9, 18)


def brokerage(id='broker-1', *, value=1000, code='GBP', paper=False, institution='Moomoo', when='2026-09-18', history=None):
    return {'id': id, 'name': 'Synthetic account', 'institution': institution, 'number_suffix': '1234',
            'total_value': value, 'currency': code, 'is_paper': paper, 'holdings_synced_at': when,
            'total_value_as_of': when,
            'history': {'data': history or []}}


class WealthTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE accounts(id TEXT PRIMARY KEY, bank TEXT, account_type TEXT, display_name TEXT);
            CREATE TABLE statements(id INTEGER PRIMARY KEY, account_id TEXT, period_start TEXT, period_end TEXT,
                opening_balance REAL, closing_balance REAL, reconciled INTEGER, meta TEXT);
            CREATE TABLE reference_fx(date TEXT,currency TEXT,rate_to_gbp REAL,source TEXT,observed_date TEXT,fetched_at TEXT,source_url TEXT,PRIMARY KEY(date,currency));
        ''')
        self.cached = {'accounts': [], 'connections': {'data': []}, 'retrieved_at': '2026-09-18', 'cache': {}}
        self.observations = []
        self.estimates = []
        p = patch.object(wealth.snaptrade_store, 'load_portfolio', side_effect=lambda **_: self.cached)
        p.start(); self.addCleanup(p.stop)
        p = patch.object(wealth.snaptrade_store, 'list_account_estimates', side_effect=lambda **_: self.estimates)
        p.start(); self.addCleanup(p.stop)
        p = patch.object(wealth.snaptrade_store, 'list_account_observations', side_effect=lambda **_: self.observations)
        p.start(); self.addCleanup(p.stop)

    def statement(self, id='cash', *, kind='debit', bank='HSBC', value=100, when='2026-09-18', meta=None, opening=None, start=None):
        import json
        self.conn.execute('INSERT OR IGNORE INTO accounts VALUES(?,?,?,?)', (id, bank, kind, 'Synthetic ' + id))
        self.conn.execute('INSERT INTO statements(account_id,period_start,period_end,opening_balance,closing_balance,reconciled,meta) VALUES(?,?,?,?,?,1,?)',
                          (id, start, when, opening, value, json.dumps(meta or {})))
        self.conn.commit()

    def data(self, **kwargs):
        return wealth.bundle(self.conn, today=TODAY, **kwargs)

    def fx(self, code='USD', rate=0.75, when='2026-09-18'):
        self.conn.execute('INSERT OR REPLACE INTO reference_fx VALUES(?,?,?,\'Test ECB\',?,?,NULL)', (when, code, rate, when, when))
        self.conn.commit()

    def test_assets_less_debt_and_overpaid_card_is_asset(self):
        self.statement(value=1000)
        self.statement('debt', kind='credit', value=200)
        self.statement('overpaid', kind='credit', value=-25)
        s = self.data()['summary']
        self.assertEqual(s['net_worth'], 825)
        self.assertEqual(s['assets'], 1025)
        self.assertEqual(s['liabilities'], 200)
        self.assertFalse(s['partial'])

    def test_bank_only_explanations_omit_unrelated_brokerage_and_credit_guidance(self):
        self.statement(value=100)
        data = self.data()
        details = ' '.join(data['methodology'])
        self.assertIn('bank statements', details)
        self.assertNotIn('SnapTrade', details)
        self.assertNotIn('Brokerage', details)
        self.assertNotIn('Credit balances', details)
        self.assertFalse(any('connected portfolio' in warning for warning in data['warnings']))

    def test_reconciled_printed_bank_balances_extend_old_history_without_changing_current(self):
        self.conn.execute('CREATE TABLE transactions(id INTEGER PRIMARY KEY,statement_id INTEGER,txn_date TEXT,amount REAL,balance REAL)')
        self.statement(value=95, opening=100, start='2024-01-01', when='2024-01-31')
        statement_id = self.conn.execute('SELECT id FROM statements').fetchone()[0]
        self.conn.executemany('INSERT INTO transactions(statement_id,txn_date,amount,balance) VALUES(?,?,?,?)', [
            (statement_id, '2024-01-05', -10, 90),
            (statement_id, '2024-01-06', 5, 95),
        ])
        accounts, _ = wealth.sources(self.conn, today=TODAY)
        points = {point['date']: point['value'] for point in accounts[0]['observations']}
        self.assertEqual(points['2024-01-05'], 90)
        self.assertEqual(points['2024-01-06'], 95)
        self.assertEqual(self.data()['summary']['net_worth'], 95)
        self.conn.execute('UPDATE transactions SET balance=91 WHERE txn_date=?', ('2024-01-05',))
        accounts, _ = wealth.sources(self.conn, today=TODAY)
        self.assertNotIn('2024-01-05', {point['date'] for point in accounts[0]['observations']})
        self.assertEqual(self.data()['summary']['net_worth'], 95)

    def test_connected_total_includes_cash_without_adding_imported_broker(self):
        self.statement('imported', kind='investment', bank='Moomoo', value=None,
                       meta={'parser': 'moomoo', 'base_currency': 'GBP', 'net_asset_value': 900, 'native_reconciled': True})
        self.cached['accounts'] = [brokerage(value=1000)]
        accounts, _ = wealth.sources(self.conn, today=TODAY)
        source_reconciliation.set_link(self.conn, source_id='statement:imported', canonical_id='connected:broker-1',
            evidence_kind='verified_statement_account_id', evidence_digest='a' * 64, accounts=accounts)
        data = self.data()
        self.assertEqual(data['summary']['net_worth'], 1000)
        self.assertEqual(data['selected_account_ids'], ['connected:broker-1'])
        self.assertFalse(data['accounts'][0]['default_included'])
        explicit = self.data(account_ids=['statement:imported', 'connected:broker-1'])
        self.assertEqual(explicit['summary']['net_worth'], 1000)
        self.assertEqual(explicit['selected_account_ids'], ['connected:broker-1'])
        self.assertFalse(any('same money' in w for w in explicit['warnings']))

    def test_activity_only_archive_is_excluded_but_institution_alone_never_excludes_supported_nav(self):
        self.statement('isa', kind='investment', bank='Trading212', value=None,
                       meta={'parser': 'trading212', 'account_kind': 'isa'})
        self.statement('moomoo', kind='investment', bank='Moomoo', value=None,
                       meta={'parser': 'moomoo', 'base_currency': 'GBP', 'net_asset_value': 900, 'native_reconciled': True})
        self.cached['accounts'] = [brokerage(institution='Trading212'), brokerage('other', institution='Moomoo')]
        result = self.data()
        by_id = {account['id']: account for account in result['accounts']}
        self.assertFalse(by_id['statement:isa']['default_included'])
        self.assertIn('Statement activity only', by_id['statement:isa']['exclusion_reason'])
        self.assertNotIn('statement:isa', result['selected_account_ids'])
        self.assertIn('statement:moomoo', result['selected_account_ids'])
        self.assertEqual(result['summary']['net_worth'], 2900)
        self.assertEqual(result['summary']['missing_accounts'], 0)
        explicit = self.data(account_ids=['statement:isa'])
        self.assertIsNone(explicit['summary']['net_worth'])
        self.assertEqual(explicit['summary']['missing_accounts'], 1)

    def test_zero_supported_investment_nav_remains_included(self):
        self.statement('separate-account', kind='investment', bank='Moomoo', value=None,
            meta={'parser': 'moomoo', 'base_currency': 'GBP', 'net_asset_value': 0, 'native_reconciled': True})
        self.cached['accounts'] = [brokerage(institution='Moomoo')]
        result = self.data()
        imported = next(account for account in result['accounts'] if account['id'] == 'statement:separate-account')
        self.assertTrue(imported['default_included'])
        self.assertEqual(imported['native_value'], 0)
        self.assertEqual(result['summary']['missing_accounts'], 0)

    def test_history_uses_stable_cohort_and_current_only_account_never_creates_a_jump(self):
        self.cached['accounts'] = [brokerage(value=120, history=[
            {'date': '2026-08-01', 'total_value': 100, 'currency': 'GBP'},
            {'date': '2026-09-01', 'total_value': 110, 'currency': 'GBP'}]), brokerage('new', value=300)]
        result = self.data()
        self.assertEqual(result['summary']['net_worth'], 420)
        self.assertIsNone(result['summary']['change'])
        history = result['history']['cohort']
        self.assertEqual(history['account_ids'], ['connected:broker-1'])
        self.assertEqual(history['excluded_account_ids'], ['connected:new'])
        self.assertEqual([p['net_worth'] for p in history['points'] if p['complete']], [100, 110, 120])
        self.assertEqual(history['current_value'], 120)
        self.assertEqual(history['net_start_date'], TODAY.isoformat())
        self.assertEqual([point['net_worth'] for point in history['net_points']], [420])
        self.assertEqual(history['net_points'][0]['known_accounts'], 2)
        self.assertEqual(history['net_points'][0]['net_worth'], result['summary']['net_worth'])
        self.assertIsNone(history['change'])
        self.assertFalse(history['change_comparable'])
        self.assertTrue(history['current_shown_separately'])
        self.assertTrue(all(p['known_accounts'] == 1 for p in history['points'] if p['complete']))
        self.assertTrue(any(not p['complete'] and p['net_worth'] is None for p in history['points']))
        self.assertEqual(result['points'][-1]['net_worth'], 420)

    def test_current_only_selection_has_no_invented_history(self):
        self.cached['accounts'] = [brokerage(value=120)]
        result = self.data()
        self.assertEqual(result['summary']['net_worth'], 120)
        self.assertEqual(result['history']['cohort']['points'], [])
        self.assertEqual(result['history']['cohort']['net_start_date'], TODAY.isoformat())
        self.assertEqual(result['history']['cohort']['net_points'][0]['net_worth'], 120)
        self.assertIsNone(result['history']['cohort']['change'])
        self.assertEqual(result['history']['cohort']['account_count'], 0)

    def test_small_short_history_cannot_compress_the_main_asset_trend(self):
        self.cached['accounts'] = [brokerage('main', value=100000, history=[
            {'date': f'2026-09-{day:02}', 'total_value': 100000, 'currency': 'GBP'} for day in range(1, 18)]),
            brokerage('small', value=100, history=[
                {'date': f'2026-09-{day:02}', 'total_value': 100, 'currency': 'GBP'} for day in (10, 11)])]
        result = self.data(range_name='1m')
        self.assertEqual(result['summary']['net_worth'], 100100)
        self.assertEqual(result['history']['cohort']['account_ids'], ['connected:main'])
        self.assertEqual(result['history']['cohort']['start_date'], '2026-09-01')
        self.assertEqual(result['history']['cohort']['end_date'], '2026-09-18')
        self.assertEqual(len(result['history']['cohort']['points']), 18)

    def test_linked_statement_extends_canonical_history_and_can_be_reversed(self):
        self.statement('imported', kind='investment', bank='Moomoo', when='2026-01-01', value=None,
                       meta={'parser': 'moomoo', 'base_currency': 'GBP', 'net_asset_value': 900, 'native_reconciled': True})
        self.cached['accounts'] = [brokerage(value=1000)]
        accounts, _ = wealth.sources(self.conn, today=TODAY)
        source_reconciliation.set_link(self.conn, source_id='statement:imported', canonical_id='connected:broker-1',
            evidence_kind='verified_statement_account_id', evidence_digest='a' * 64, accounts=accounts)
        result = self.data(account_ids=['statement:imported'])
        self.assertEqual(result['selected_account_ids'], ['connected:broker-1'])
        self.assertEqual([p['net_worth'] for p in result['history']['cohort']['points'] if p['complete']], [900, 1000])
        self.assertEqual(result['accounts'][1]['source_ids'], ['statement:imported', 'connected:broker-1'])
        self.assertEqual(result['accounts'][1]['drilldown'], {'page': 'connected-portfolio', 'account_id': 'broker-1'})
        source_reconciliation.remove_link(self.conn, 'statement:imported')
        reverted = self.data()
        self.assertEqual(reverted['summary']['net_worth'], 1900)
        self.assertTrue(all(a['linked_to'] is None for a in reverted['accounts']))

    def test_history_fx_gaps_are_null_not_a_changing_subtotal(self):
        self.cached['accounts'] = [brokerage(value=120, code='USD', history=[
            {'date': '2026-09-01', 'total_value': 100, 'currency': 'USD'},
            {'date': '2026-09-12', 'total_value': 110, 'currency': 'USD'},
            {'date': '2026-09-17', 'total_value': 115, 'currency': 'USD'}])]
        self.fx(when='2026-09-01')
        self.fx(when='2026-09-17')
        result = self.data()
        self.assertEqual(result['history']['cohort']['account_ids'], ['connected:broker-1'])
        points = {p['date']: p for p in result['history']['cohort']['points']}
        self.assertEqual([points[d]['net_worth'] for d in ('2026-09-01', '2026-09-12', '2026-09-17')], [75, None, 86.25])
        self.assertEqual(result['history']['cohort']['current_value'], 90)
        self.assertFalse(points['2026-09-12']['complete'])

    def test_cohort_history_never_carries_a_stale_tail_indefinitely(self):
        self.cached['accounts'] = [brokerage(value=120, when='2026-09-05', history=[
            {'date': '2026-09-01', 'total_value': 100, 'currency': 'GBP'},
            {'date': '2026-09-03', 'total_value': 110, 'currency': 'GBP'}])]
        result = self.data()
        self.assertEqual(result['summary']['net_worth'], 120)
        self.assertTrue(result['summary']['partial'])
        self.assertEqual(result['history']['cohort']['end_date'], '2026-09-05')
        self.assertEqual(result['history']['cohort']['end_value'], 120)
        self.assertEqual([p['date'] for p in result['history']['cohort']['points']], ['2026-09-01', '2026-09-03', '2026-09-05'])

    def test_zero_accounts_and_stale_sources_have_no_manufactured_recent_trend(self):
        self.assertEqual(self.data()['history']['cohort']['points'], [])
        self.assertIsNone(self.data()['summary']['net_worth'])
        self.cached['accounts'] = [brokerage(value=120, when='2026-02-01', history=[
            {'date': '2026-01-01', 'total_value': 100, 'currency': 'GBP'}])]
        result = self.data(range_name='1m')
        self.assertEqual(result['history']['cohort']['points'], [])
        self.assertEqual(result['summary']['net_worth'], 120)

    def test_paper_default_exclusion_and_no_selection_never_fake_zero(self):
        self.cached['accounts'] = [brokerage(paper=True)]
        result = self.data()
        self.assertIsNone(result['summary']['net_worth'])
        self.assertEqual(result['summary']['selected_accounts'], 0)
        selected = self.data(account_ids=['connected:broker-1'])
        self.assertEqual(selected['summary']['net_worth'], 1000)
        self.assertTrue(any('simulated' in w for w in selected['warnings']))
        self.assertIsNone(self.data(account_ids=['__none__'])['summary']['net_worth'])

    def test_unknown_total_and_missing_fx_remain_unknown(self):
        self.cached['accounts'] = [brokerage(value=None), brokerage('usd', code='USD')]
        s = self.data()['summary']
        self.assertIsNone(s['net_worth'])
        self.assertEqual(s['missing_accounts'], 2)
        self.assertTrue(s['partial'])

    def test_dated_fx_no_future_rate_or_unbounded_carry(self):
        self.cached['accounts'] = [brokerage(code='USD', history=[{'date': '2026-08-01', 'total_value': 900, 'currency': 'USD'}])]
        self.fx(when='2026-09-18')
        result = self.data()
        self.assertEqual(result['summary']['net_worth'], 750)
        self.assertIsNone(result['points'][0]['net_worth'])
        self.assertIsNone(result['summary']['change'])
        self.assertTrue(result['points'][-1]['estimated'])
        self.conn.execute('DELETE FROM reference_fx')
        self.fx(when='2026-09-10')
        self.assertIsNone(self.data()['summary']['net_worth'])

    def test_cross_currency_conversion_and_same_currency_no_rate_needed(self):
        self.cached['accounts'] = [brokerage(code='USD', value=100), brokerage('sgd', code='SGD', value=100)]
        self.fx(code='USD', rate=.75)
        self.fx(code='SGD', rate=.60)
        self.assertEqual(self.data(currency_code='SGD')['summary']['net_worth'], 225)
        self.conn.execute('DELETE FROM reference_fx')
        direct = self.data(currency_code='USD', account_ids=['connected:broker-1'])
        self.assertEqual(direct['summary']['net_worth'], 100)

    def test_recorded_priority_over_estimate_and_new_coverage_suppresses_change(self):
        self.statement(value=100, when='2026-08-01')
        self.cached['accounts'] = [brokerage(value=200, history=[{'date': '2026-09-18', 'total_value': 199, 'currency': 'GBP'}])]
        result = self.data()
        self.assertEqual(result['summary']['net_worth'], 300)
        self.assertEqual(result['points'][0]['net_worth'], 100)
        self.assertEqual(result['points'][0]['missing_accounts'], 1)
        self.assertEqual(result['points'][-1]['carried_accounts'], 1)
        self.assertIsNone(result['summary']['change'])
        self.assertFalse(result['summary']['change_comparable'])

    def test_same_coverage_change_is_value_change_and_reconciled_opening_is_anchor(self):
        self.statement(value=140, when='2026-09-18', opening=100, start='2026-08-10')
        result = self.data()
        self.assertEqual(result['points'][0]['date'], '2026-08-09')
        self.assertEqual(result['summary']['change'], 40)
        self.assertEqual(result['summary']['change_pct'], 40)
        self.assertTrue(result['summary']['change_comparable'])
        self.assertTrue(any('not investment return' in m for m in result['methodology']))

    def test_invalid_selection_and_future_statement_are_not_valued(self):
        self.statement(when='2027-01-01')
        self.assertIsNone(self.data()['summary']['net_worth'])
        for kwargs in ({'account_ids': ['anything']}, {'currency_code': 'GBp'}, {'range_name': '100y'}):
            with self.assertRaises(ValueError):
                self.data(**kwargs)

    def test_explicit_fx_refresh_sends_only_currency_dates_and_persists(self):
        self.cached['accounts'] = [brokerage(code='SGD')]
        received = []
        def fake(items):
            received.extend(items)
            return {(code, when): {'currency': code, 'date': when, 'observed_date': when,
                    'rate_to_gbp': .6, 'source': 'Test ECB', 'fetched_at': when} for code, when in items}
        result = wealth.refresh_fx(self.conn, today=TODAY, fetch=fake)
        self.assertIn(('SGD', '2026-09-18'), received)
        self.assertIn(('SGD', '2026-08-19'), received)
        self.assertTrue(all(code == 'SGD' and len(when) == 10 for code, when in received))
        self.assertEqual(result['updated'], 6)
        self.assertEqual(self.data()['summary']['net_worth'], 600)
        received.clear()
        wealth.refresh_fx(self.conn, today=TODAY, fetch=fake)
        self.assertEqual(received, [])

    def test_archived_history_retained_without_current_value(self):
        self.observations = [{'account_id': 'old', 'name': 'Old broker', 'institution': 'Broker',
            'currency': 'GBP', 'total_value': 200, 'observed_at': '2026-08-01', 'retrieved_at': '2026-08-01'}]
        result = self.data()
        self.assertEqual(result['accounts'][0]['history_points'], 1)
        self.assertFalse(result['accounts'][0]['default_included'])
        selected = self.data(account_ids=['connected:old'])
        self.assertEqual(selected['points'][0]['net_worth'], 200)
        self.assertIsNone(selected['summary']['net_worth'])

    def test_fx_refresh_includes_foreign_holdings_and_cash_in_a_gbp_account(self):
        account = brokerage(code='GBP')
        account['positions'] = {'data': [{'currency': 'USD', 'listing_currency': 'SGD'}]}
        account['balances'] = {'data': [{'currency': 'EUR'}]}
        self.cached['accounts'] = [account]
        received = []
        def fake(items):
            received.extend(items)
            return {}
        wealth.refresh_fx(self.conn, today=TODAY, fetch=fake)
        self.assertEqual({code for code, _ in received}, {'USD', 'SGD', 'EUR'})
        self.assertTrue(all(len(when) == 10 for _, when in received))

    def test_persisted_estimates_survive_provider_window_and_latest_unknown(self):
        self.cached['accounts'] = [brokerage(value=None)]
        self.estimates = [{'account_id': 'broker-1', 'name': 'Saved', 'institution': 'Moomoo',
            'date': '2025-01-01', 'total_value': 800, 'currency': 'GBP'}]
        result = self.data()
        self.assertEqual(result['points'][0]['net_worth'], 800)
        self.assertIsNone(result['summary']['net_worth'])

    def test_conflicting_same_day_statement_values_are_withheld(self):
        self.statement(value=100)
        self.statement(value=200)
        result = self.data()
        self.assertIsNone(result['summary']['net_worth'])
        self.assertTrue(result['summary']['partial'])

    def test_imported_broker_alone_does_not_warn_of_duplicate_selected_sources(self):
        self.statement('imported', kind='investment', bank='Moomoo', value=None,
            meta={'parser': 'moomoo', 'base_currency': 'GBP', 'net_asset_value': 900, 'native_reconciled': True})
        self.cached['accounts'] = [brokerage()]
        result = self.data(account_ids=['statement:imported'])
        self.assertFalse(any('same money' in w for w in result['warnings']))


class WealthAccessTests(unittest.TestCase):
    def test_existing_auth_wall_and_cross_site_protection_cover_new_routes(self):
        import os
        from fastapi.testclient import TestClient
        from app import main, wealth_routes
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            'FINANCE_AUTH_ENABLED': 'true', 'FINANCE_ALLOWED_HOSTS': 'testserver',
            'FINANCE_AUTH_DB_PATH': str(Path(tmp) / 'auth.db'),
            'FINANCE_AUTH_KEY_PATH': str(Path(tmp) / 'keys' / 'auth.key'),
        }), patch.object(main, 'DB_PATH', Path(tmp) / 'missing.db'), patch.object(wealth_routes, 'connection') as connection:
            with TestClient(main.app) as client:
                for path, method in (('/api/wealth', 'get'), ('/api/wealth/sources', 'get'), ('/api/wealth/refresh', 'post')):
                    response = getattr(client, method)(path, headers={'X-Ledger-Request': '1'})
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.headers['cache-control'], 'no-store')
                with patch.dict(os.environ, {'FINANCE_AUTH_ENABLED': 'false', 'FINANCE_DEPLOYMENT': 'trusted-lan'}):
                    response = client.post('/api/wealth/refresh', headers={'Origin': 'https://other.example'})
                    self.assertEqual(response.status_code, 403)
            connection.assert_not_called()


if __name__ == '__main__':
    unittest.main()

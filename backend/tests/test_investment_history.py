"""Independent daily valuation arithmetic; synthetic data only, no network."""
from datetime import date
import json
import sqlite3
import unittest
from unittest.mock import patch

from app.investment_history import reconstruct, _instruments, _observation, _saved_endpoints


class Rates:
    def convert(self, amount, original, target, when):
        if amount is None or original is None:
            return None
        if original == 'GBP':
            return amount
        if original == 'USD':
            return amount * (.8 if when < '2026-09-02' else .9)
        return None


class InvestmentHistoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE accounts(id TEXT PRIMARY KEY, bank TEXT, account_type TEXT);
            CREATE TABLE statements(id INTEGER PRIMARY KEY,account_id TEXT,period_start TEXT,period_end TEXT,meta TEXT);
            CREATE TABLE transactions(id INTEGER PRIMARY KEY,account_id TEXT,txn_date TEXT,type_code TEXT,meta TEXT);
            CREATE TABLE corporate_actions(account_id TEXT,isin TEXT,ex_date TEXT,ratio_new INTEGER,ratio_old INTEGER);
        ''')
        self.position = {'id': 'p', 'instrument_id': 'instrument', 'raw_symbol': 'ABC', 'symbol': 'ABC',
                         'quantity': 20, 'listing_currency': 'USD', 'exchange': 'XNYS', 'asset_type': 'stock'}
        self.cached = {'accounts': [{'id': 'broker', 'institution': 'Moomoo', 'positions': {'status': 'ok', 'data': [self.position]}}]}
        self.markets = {'instrument': {'symbol': 'ABC', 'currency': 'USD', 'exchange': 'NYQ', 'status': 'ok',
                       'split_adjusted': True, 'split_events_complete': True, 'splits': [],
                       'points': [{'date': '2026-09-01', 'close': 100}, {'date': '2026-09-02', 'close': 110}, {'date': '2026-09-03', 'close': 120}]}}

    def event(self, when, kind, quantity=None, cash=None):
        self.conn.execute('INSERT INTO transactions(account_id,txn_date,type_code,meta) VALUES(?,?,?,?)',
            ('statement-account', when, kind, json.dumps({'time': when, 'action': kind, 'isin': 'test-isin', 'ticker': 'ABC',
                'shares': quantity, 'cash_movements': cash if cash is not None else {}, 'execution_currency_major': 'USD',
                'source_statement_end': '2026-09-03'})))

    def moomoo(self, opening=10, closing=10, initial_cash=50, final_cash=50):
        self.conn.execute("INSERT INTO accounts VALUES('statement-account','Moomoo','investment')")
        identity = {'isin': 'test-isin', 'ticker': 'ABC', 'currency': 'USD', 'exchange': 'US'}
        self.conn.execute('INSERT INTO statements(account_id,period_start,period_end,meta) VALUES(?,?,?,?)',
            ('statement-account', '2026-09-01', '2026-09-03', json.dumps({'parser': 'moomoo', 'native_reconciled': True,
                'position_changes': [{**identity, 'starting_shares': opening}], 'positions': [{**identity, 'shares': closing}],
                'opening_cash_balances': {'USD': initial_cash}, 'cash_balances': {'USD': final_cash}})))

    def run_history(self):
        return reconstruct(self.conn, cached=self.cached, markets=self.markets, rates=Rates(),
                           today=date(2026, 9, 3), cutoff='2026-09-01')['sources']['statement:statement-account']

    def test_daily_prices_and_historical_fx_value_nonzero_opening_shares(self):
        self.moomoo()
        result = self.run_history()
        self.assertEqual([row['value'] for row in result['observations']], [840, 1035, 1125])
        self.assertEqual(result['coverage']['reconciled_periods'], 1)
        self.assertTrue(all(row['valuation_basis'] == 'daily_holdings' and row['estimated'] for row in result['observations']))

    def test_buy_changes_actual_shares_and_cash_on_event_day(self):
        self.moomoo(closing=12, initial_cash=300, final_cash=80)
        self.event('2026-09-02', 'Market buy', 2, {'USD': -220})
        self.assertEqual([row['value'] for row in self.run_history()['observations']], [1040, 1260, 1368])

    def test_month_requires_both_share_and_cash_reconciliation(self):
        self.moomoo(closing=12)
        self.assertEqual(self.run_history()['observations'], [])
        self.event('2026-09-02', 'Market buy', 2, {'USD': -220})
        self.assertEqual(self.run_history()['observations'], [])

    def test_split_adjusted_closes_restore_contemporaneous_units(self):
        self.moomoo(closing=20, initial_cash=0, final_cash=0)
        self.event('2026-09-02', 'Stock split', 10, {})
        market = self.markets['instrument']
        market.update(splits=[{'date': '2026-09-02', 'numerator': 2, 'denominator': 1}],
                      points=[{'date': '2026-09-01', 'close': 50}, {'date': '2026-09-02', 'close': 55}, {'date': '2026-09-03', 'close': 60}])
        self.assertEqual([row['value'] for row in self.run_history()['observations']], [800, 990, 1080])
        del market['split_events_complete']
        self.assertTrue(all(row['value'] is None for row in self.run_history()['observations']))

    def test_wrong_venue_missing_currency_and_future_prices_never_fill_history(self):
        self.moomoo()
        self.markets['instrument']['exchange'] = 'LSE'
        self.assertTrue(all(row['value'] is None for row in self.run_history()['observations']))
        self.markets['instrument']['exchange'] = 'NYQ'
        self.markets['instrument']['points'] = [{'date': '2026-09-03', 'close': 100}]
        self.assertEqual([row['value'] for row in self.run_history()['observations']], [None, None, 945])

    def test_next_weekday_cash_recognition_recovers_month_with_original_posting_unchanged(self):
        self.moomoo(final_cash=55)
        self.event('2026-09-04', 'Dividend', None, {'USD': 5})
        original = tuple(self.conn.execute('SELECT txn_date,meta FROM transactions').fetchone())
        writes = self.conn.total_changes
        result = self.run_history()
        self.assertEqual([point['value'] for point in result['observations']],[840,1035,1129.5])
        self.assertTrue(all(point['cash_timing_estimated'] for point in result['observations']))
        self.assertEqual(result['coverage']['cash_timing_estimated_periods'],1)
        self.assertEqual(result['coverage']['cash_timing_adjustments'][0]['posted_date'],'2026-09-04')
        self.assertEqual(result['coverage']['cash_timing_adjustments'][0]['statement_date'],'2026-09-03')
        self.assertIn('exact earlier economic recognition date is not established',result['issues'][0])
        self.assertEqual(tuple(self.conn.execute('SELECT txn_date,meta FROM transactions').fetchone()),original)
        self.assertEqual(self.conn.total_changes,writes)

    def test_cash_recognition_still_requires_independent_closing_reconciliation(self):
        self.moomoo(final_cash=54)
        self.event('2026-09-04','Dividend',None,{'USD':5})
        result = self.run_history()
        self.assertEqual(result['observations'],[])
        self.assertIn('do not reconcile',result['issues'][0])

    def test_out_of_period_trade_and_unbounded_cash_timing_are_not_shifted(self):
        self.moomoo(closing=11,final_cash=45)
        self.event('2026-09-04','Market buy',1,{'USD':-5})
        self.assertEqual(self.run_history()['observations'],[])
        self.conn.execute('DELETE FROM transactions')
        self.event('2026-09-07','Dividend',None,{'USD':-5})
        self.assertEqual(self.run_history()['observations'],[])
        self.conn.execute('DELETE FROM transactions')
        self.event('2026-09-04','Deposit',None,{'USD':-5})
        self.assertEqual(self.run_history()['observations'],[])

    def test_recognized_cash_is_not_added_again_in_next_period(self):
        self.moomoo(final_cash=55)
        self.event('2026-09-04','Dividend',None,{'USD':5})
        meta = json.loads(self.conn.execute('SELECT meta FROM statements').fetchone()[0])
        meta['opening_cash_balances'] = meta['cash_balances']
        self.conn.execute('INSERT INTO statements(account_id,period_start,period_end,meta) VALUES(?,?,?,?)',
            ('statement-account','2026-09-04','2026-09-06',json.dumps(meta)))
        result = reconstruct(self.conn,cached=self.cached,markets=self.markets,rates=Rates(),
                             today=date(2026,9,6),cutoff='2026-09-01')['sources']['statement:statement-account']
        self.assertEqual([point['value'] for point in result['observations']],[840,1035,1129.5,1129.5,1129.5,1129.5])
        self.assertEqual(result['coverage']['cash_timing_estimated_periods'],1)

    def test_daily_and_monthly_scopes_share_one_execution_without_replaying_it_twice(self):
        self.moomoo(closing=12, initial_cash=300, final_cash=80)
        self.event('2026-09-02', 'Market buy', 2, {'USD': -220})
        event = json.loads(self.conn.execute('SELECT meta FROM transactions').fetchone()[0])
        event['source_statement_end'] = '2026-09-02'  # First imported daily evidence remains original.
        event['source_observations'] = [
            {'source_statement_start': '2026-09-02', 'source_statement_end': '2026-09-02'},
            {'source_statement_start': '2026-09-01', 'source_statement_end': '2026-09-03'},
        ]
        self.conn.execute('UPDATE transactions SET meta=?', (json.dumps(event),))
        monthly = json.loads(self.conn.execute('SELECT meta FROM statements').fetchone()[0])
        self.conn.execute('INSERT INTO statements(account_id,period_start,period_end,meta) VALUES(?,?,?,?)',
            ('statement-account', '2026-09-02', '2026-09-02', json.dumps({**monthly, 'statement_type': 'daily'})))
        daily = json.loads(json.dumps(monthly))
        daily['position_changes'][0]['starting_shares'] = 12
        daily['opening_cash_balances'] = {'USD': 80}
        daily['statement_type'] = 'daily'
        self.conn.execute('INSERT INTO statements(account_id,period_start,period_end,meta) VALUES(?,?,?,?)',
            ('statement-account', '2026-09-03', '2026-09-03', json.dumps(daily)))
        original = tuple(self.conn.execute('SELECT txn_date,meta FROM transactions').fetchone())
        result = self.run_history()
        self.assertEqual([row['value'] for row in result['observations']], [1040, 1260, 1368])
        self.assertEqual(result['coverage']['reconciled_periods'], 3)
        self.assertFalse(any('reconcile' in issue for issue in result['issues']))
        self.assertEqual(tuple(self.conn.execute('SELECT txn_date,meta FROM transactions').fetchone()), original)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 1)

    def test_overlapping_monthly_replay_keeps_daily_value_within_printed_cash_rounding(self):
        self.moomoo()
        daily = json.loads(self.conn.execute('SELECT meta FROM statements').fetchone()[0])
        daily.update(statement_type='daily', opening_cash_balances={'USD': 50}, cash_balances={'USD': 50})
        self.conn.execute("UPDATE statements SET period_end='2026-09-01',meta=?", (json.dumps(daily),))
        monthly = json.loads(json.dumps(daily))
        monthly.pop('statement_type')
        monthly['opening_cash_balances'] = {'USD': 50.5}
        monthly['cash_balances'] = {'USD': 50.5}
        self.conn.execute('INSERT INTO statements(account_id,period_start,period_end,meta) VALUES(?,?,?,?)',
                          ('statement-account','2026-09-01','2026-09-03',json.dumps(monthly)))
        result = self.run_history()
        self.assertEqual([point['date'] for point in result['observations']],
                         ['2026-09-01','2026-09-02','2026-09-03'])
        self.assertEqual(result['observations'][0]['value'], 840)
        self.assertEqual(result['observations'][0]['source_statement_type'], 'daily')
        self.assertEqual(result['observations'][1]['source_statement_type'], 'monthly')
        self.assertTrue(result['observations'][1]['overlaps_daily_reports'])
        self.assertFalse(any('overlapping statement periods disagree' in issue for issue in result['issues']))

        monthly['opening_cash_balances'] = {'USD': 52}
        monthly['cash_balances'] = {'USD': 52}
        self.conn.execute("UPDATE statements SET meta=? WHERE period_end='2026-09-03'", (json.dumps(monthly),))
        conflict = self.run_history()
        self.assertIsNone(conflict['observations'][0]['value'])
        self.assertTrue(any('overlapping statement periods disagree' in issue for issue in conflict['issues']))

    def test_scoped_event_assignment_works_without_legacy_end_and_rejects_incomplete_scope(self):
        self.moomoo(closing=12, initial_cash=300, final_cash=80)
        self.event('2026-09-02', 'Market buy', 2, {'USD': -220})
        event = json.loads(self.conn.execute('SELECT meta FROM transactions').fetchone()[0])
        event.pop('source_statement_end')
        event['source_observations'] = [{'source_statement_start': '2026-09-01', 'source_statement_end': '2026-09-03'}]
        self.conn.execute('UPDATE transactions SET meta=?', (json.dumps(event),))
        self.assertEqual([row['value'] for row in self.run_history()['observations']], [1040, 1260, 1368])
        event['source_observations'][0].pop('source_statement_start')
        self.conn.execute('UPDATE transactions SET meta=?', (json.dumps(event),))
        result = self.run_history()
        self.assertEqual(result['observations'], [])
        self.assertTrue(any('without a statement assignment' in issue for issue in result['issues']))

    def test_separate_daily_intervals_do_not_fill_missing_statement_dates(self):
        self.moomoo()
        self.conn.execute("UPDATE statements SET period_end='2026-09-01'")
        daily = json.loads(self.conn.execute('SELECT meta FROM statements').fetchone()[0])
        daily['statement_type'] = 'daily'
        self.conn.execute('INSERT INTO statements(account_id,period_start,period_end,meta) VALUES(?,?,?,?)',
            ('statement-account', '2026-09-03', '2026-09-03', json.dumps(daily)))
        result = self.run_history()
        self.assertEqual([row['date'] for row in result['observations']], ['2026-09-01', '2026-09-03'])
        self.assertEqual(result['coverage']['reconciled_periods'], 2)

    def test_friday_statement_can_recognize_monday_posting_with_weekend_gap(self):
        self.moomoo(final_cash=55)
        self.conn.execute("UPDATE statements SET period_end='2026-09-04'")
        self.event('2026-09-07','Dividend',None,{'USD':5})
        self.conn.execute("UPDATE transactions SET meta=json_set(meta,'$.source_statement_end','2026-09-04')")
        result = reconstruct(self.conn,cached=self.cached,markets=self.markets,rates=Rates(),
                             today=date(2026,9,4),cutoff='2026-09-01')['sources']['statement:statement-account']
        self.assertEqual([point['value'] for point in result['observations']],[840,1035,1125,1129.5])
        self.assertEqual(result['coverage']['cash_timing_adjustments'][0]['posted_date'],'2026-09-07')

    def t212(self):
        self.conn.execute("INSERT INTO accounts VALUES('statement-account','Trading212','investment')")
        self.conn.execute("INSERT INTO statements(account_id,period_start,period_end,meta) VALUES('statement-account','2026-09-01','2026-09-03','{}')")
        self.event('2026-09-01', 'Deposit', None, {'USD': 2050})
        self.event('2026-09-01', 'Market buy', 20, {'USD': -2000})
        account = self.cached['accounts'][0]
        account.update(institution='Trading212', holdings_synced_at='2026-09-03',
                       activities={'status': 'empty', 'data': []}, balances={'status': 'ok', 'data': [{'currency': 'USD', 'cash': 0}]})

    def test_t212_exact_share_endpoint_allows_explicit_estimated_cash_without_linking(self):
        self.t212()
        result = self.run_history()
        self.assertEqual(result['coverage']['reconciled_holdings'], 1)
        self.assertEqual(result['coverage']['cash_residual'], {'USD': 50})
        self.assertEqual(result['coverage']['quantity_matched_account_id'], 'broker')
        self.assertFalse(result['coverage']['cash_reconciled'])
        self.assertTrue(result['coverage']['identity_link_required'])
        self.assertEqual(result['observations'][-1]['value'], 2205)
        self.assertFalse(self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='wealth_source_links'").fetchone())

    def test_t212_mismatched_or_ambiguous_endpoint_withholds_history(self):
        self.t212()
        self.position['quantity'] = 19
        mismatched = self.run_history()
        self.assertEqual(mismatched['observations'], [])
        self.assertNotIn('quantity_matched_account_id', mismatched['coverage'])
        self.position['quantity'] = 20
        self.cached['accounts'].append({**self.cached['accounts'][0], 'id': 'second-account'})
        ambiguous = self.run_history()
        self.assertEqual(ambiguous['observations'], [])
        self.assertNotIn('quantity_matched_account_id', ambiguous['coverage'])

    def test_stale_price_and_unrecorded_post_export_trade_withhold_history(self):
        self.t212()
        self.cached['accounts'][0]['holdings_synced_at'] = '2026-09-04'
        self.cached['accounts'][0]['activities']['data'] = [{'date': '2026-09-04', 'type': 'BUY'}]
        self.assertEqual(self.run_history()['observations'], [])
        self.cached['accounts'][0]['activities']['data'] = []
        self.markets['instrument']['points'] = [{'date': '2026-08-20', 'close': 100}]
        self.assertTrue(all(row['value'] is None for row in self.run_history()['observations']))

    def test_incomplete_t212_cash_ledger_cannot_establish_opening(self):
        self.t212()
        self.conn.execute("UPDATE transactions SET meta=json_set(meta,'$.cash_movements.USD',5) WHERE type_code='Deposit'")
        self.assertEqual(self.run_history()['observations'], [])

    def test_missing_optional_legacy_tables_return_no_reconstruction(self):
        self.conn.execute('DROP TABLE transactions')
        result = reconstruct(self.conn, cached=self.cached, markets=self.markets, rates=Rates())
        self.assertEqual(result['sources'], {})

    def test_closed_holding_uses_reviewed_listing_only_for_exact_isin_and_currency(self):
        key = 'historical:XS2399364582:XLON:GBP'
        market = {'symbol': '5SPY.L', 'currency': 'GBP', 'exchange': 'LSE', 'status': 'ok',
                  'split_adjusted': True, 'split_events_complete': True, 'splits': [],
                  'points': [{'date': '2026-09-01', 'close': 4.5}]}
        identities = {'XS2399364582': {'ticker': '5SPY', 'currency': 'GBP'}}
        instruments = _instruments(identities, {'accounts': []}, {key: market})
        point = _observation(date(2026, 9, 1), {'XS2399364582': 10}, {}, identities, instruments, Rates())
        self.assertEqual(point['value'], 45)
        identities['XS2399364582']['currency'] = 'USD'
        self.assertEqual(_instruments(identities, {'accounts': []}, {key: market}), {})
        identities = {'wrong-isin': {'ticker': '5SPY', 'currency': 'GBP'}}
        self.assertEqual(_instruments(identities, {'accounts': []}, {key: market}), {})

    def test_saved_quantity_snapshot_keeps_supported_history_after_live_trades(self):
        self.t212()
        saved = {**self.cached['accounts'][0], 'saved_quantity_endpoint': True,
                 'positions': {'status': 'ok', 'data': [dict(self.position)]},
                 'balances': {'status': 'unsupported', 'data': []}}
        self.position['quantity'] = 30
        self.cached['accounts'][0]['holdings_synced_at'] = '2026-09-18'
        with patch('app.investment_history._saved_endpoints', return_value=[saved]):
            result = self.run_history()
        self.assertEqual(result['coverage']['reconciled_holdings'], 1)
        self.assertTrue(result['coverage']['saved_quantity_endpoint'])
        self.assertEqual(result['coverage']['quantity_matched_account_id'], 'broker')
        self.assertIsNone(result['coverage']['cash_residual'])
        self.assertEqual(result['observations'][-1]['value'], 2205)

    def saved_snapshot(self, *, holdings_clock, balance_clock, known_balance=True, modern=True):
        extra = ', total_value_as_of TEXT, holdings_observed_at TEXT' if modern else ''
        self.conn.executescript(f'''
            CREATE TABLE snaptrade_account_observations(snapshot_id INTEGER, account_id TEXT, institution TEXT,
                is_paper INTEGER, observed_at TEXT, source_time_known INTEGER, holdings_status TEXT,
                holdings_truncated INTEGER {extra});
            CREATE TABLE snaptrade_position_observations(snapshot_id INTEGER, account_id TEXT, raw_symbol TEXT,
                listing_currency TEXT, instrument_id TEXT, quantity REAL, exchange TEXT, asset_type TEXT);
        ''')
        values = [1, 'broker', 'Trading212', 0, balance_clock, int(known_balance), 'ok', 0]
        if modern:
            values.extend((balance_clock if known_balance else None, holdings_clock))
        self.conn.execute('INSERT INTO snaptrade_account_observations VALUES(' + ','.join('?' for _ in values) + ')', values)
        self.conn.execute('INSERT INTO snaptrade_position_observations VALUES(?,?,?,?,?,?,?,?)',
                          (1, 'broker', 'ABC', 'USD', 'instrument', 20, 'XNYS', 'stock'))

    def test_saved_holdings_clock_preserves_replay_when_balance_clock_is_outside_export_window(self):
        self.t212()
        self.saved_snapshot(holdings_clock='2026-09-04T12:00:00Z', balance_clock='2026-09-18T09:00:00Z')
        self.position['quantity'] = 30
        self.cached['accounts'][0]['holdings_synced_at'] = '2026-09-18'
        result = self.run_history()
        self.assertEqual(result['observations'][-1]['value'], 2205)
        self.assertTrue(result['coverage']['saved_quantity_endpoint'])
        self.assertEqual(result['coverage']['quantity_check_date'], '2026-09-04')

    def test_recent_balance_does_not_make_a_late_or_undated_holdings_snapshot_eligible(self):
        self.saved_snapshot(holdings_clock='2026-09-18T12:00:00Z', balance_clock='2026-09-04T09:00:00Z')
        self.assertEqual(_saved_endpoints(self.conn, date(2026, 9, 3)), [])
        self.conn.execute('UPDATE snaptrade_account_observations SET holdings_observed_at=NULL')
        self.assertEqual(_saved_endpoints(self.conn, date(2026, 9, 3)), [])

    def test_known_holdings_clock_does_not_require_a_known_balance_clock(self):
        self.saved_snapshot(holdings_clock='2026-09-04T12:00:00Z', balance_clock='2026-09-18T09:00:00Z', known_balance=False)
        endpoints = _saved_endpoints(self.conn, date(2026, 9, 3))
        self.assertEqual(len(endpoints), 1)
        self.assertEqual(endpoints[0]['holdings_synced_at'], '2026-09-04T12:00:00Z')

    def test_legacy_quantity_clock_is_preserved_before_and_after_additive_migration(self):
        self.saved_snapshot(holdings_clock=None, balance_clock='2026-09-04T12:00:00Z', modern=False)
        before = _saved_endpoints(self.conn, date(2026, 9, 3))
        self.assertEqual(len(before), 1)
        self.conn.execute('ALTER TABLE snaptrade_account_observations ADD COLUMN total_value_as_of TEXT')
        self.conn.execute('ALTER TABLE snaptrade_account_observations ADD COLUMN holdings_observed_at TEXT')
        self.assertEqual(_saved_endpoints(self.conn, date(2026, 9, 3)), before)
        self.conn.execute('UPDATE snaptrade_account_observations SET source_time_known=0')
        self.assertEqual(_saved_endpoints(self.conn, date(2026, 9, 3)), [])


if __name__ == '__main__':
    unittest.main()

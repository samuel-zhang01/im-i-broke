"""Historical projections must not change today's broker-reported NAV."""
from datetime import date
import sqlite3
import unittest
from unittest.mock import patch

from app import wealth, source_reconciliation
from app.wealth_investments import add_history, recorded_cutover


class HistoryProjectionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE accounts(id TEXT PRIMARY KEY, bank TEXT, account_type TEXT, display_name TEXT);
            CREATE TABLE statements(id INTEGER PRIMARY KEY, account_id TEXT, period_start TEXT, period_end TEXT,
                opening_balance REAL, closing_balance REAL, reconciled INTEGER, meta TEXT);
            CREATE TABLE reference_fx(date TEXT,currency TEXT,rate_to_gbp REAL,source TEXT,observed_date TEXT,fetched_at TEXT,source_url TEXT,PRIMARY KEY(date,currency));
            INSERT INTO accounts VALUES('archive','Moomoo','investment','Imported brokerage');
            INSERT INTO statements VALUES(1,'archive','2026-09-01','2026-09-02',NULL,NULL,1,
                '{"parser":"moomoo","base_currency":"GBP","net_asset_value":1100,"native_reconciled":true}');
        ''')
        self.cached = {'accounts': [{'id': 'live', 'name': 'Live broker', 'institution': 'Moomoo',
            'total_value': 1200, 'currency': 'GBP', 'is_paper': False, 'holdings_synced_at': '2026-09-04',
            'total_value_as_of': '2026-09-04',
            'history': {'data': [{'date':'2026-09-01', 'total_value':500, 'currency':'GBP'},
                                 {'date':'2026-09-02', 'total_value':550, 'currency':'GBP'}]}}],
            'connections': {'data': []}, 'retrieved_at':'2026-09-04', 'cache':{}}
        self.rows = [{'date': f'2026-09-0{day}', 'value': value, 'currency': 'GBP', 'source': 'reconstructed',
                      'valuation_basis': 'daily_holdings', 'estimated': True, 'reconciled': True}
                     for day, value in [(1,1000),(2,1100)]]
        for name, value in [('load_portfolio',self.cached), ('list_account_observations',[]), ('list_account_estimates',[])]:
            mock = patch.object(wealth.snaptrade_store, name, return_value=value)
            mock.start(); self.addCleanup(mock.stop)
        mock = patch('app.investment_history.reconstruct', return_value={'sources':{
            'statement:archive': {'observations':self.rows, 'coverage': {'valued_days':2}, 'issues':[]}}})
        mock.start(); self.addCleanup(mock.stop)
        accounts, _ = wealth.sources(self.conn, today=date(2026,9,4))
        source_reconciliation.set_link(self.conn, source_id='statement:archive', canonical_id='connected:live',
            evidence_kind='owner_confirmed', evidence_digest='a'*64, accounts=accounts)

    def test_same_reconstruction_drives_both_views_and_preserves_current_value(self):
        args = {'today':date(2026,9,4), 'range_name':'1m'}
        combined = wealth.bundle(self.conn, **args)
        investments = wealth.bundle(self.conn, account_ids=['connected:live'], **args)
        self.assertEqual(combined['history']['cohort']['points'], investments['history']['points'])
        self.assertEqual(combined['summary']['net_worth'],1200)
        self.assertEqual(combined['selected_account_ids'],['connected:live'])
        points = {point['date']:point for point in combined['history']['cohort']['points']}
        self.assertEqual(points['2026-09-01']['net_worth'],1000)
        self.assertEqual(combined['points'][0]['net_worth'],1000)
        self.assertEqual(points['2026-09-02']['net_worth'],1100)
        raw_points = {point['date']:point for point in combined['points']}
        self.assertIsNone(raw_points['2026-09-03']['net_worth'])
        self.assertEqual(points['2026-09-01']['reconstructed_accounts'],1)
        self.assertNotEqual(points['2026-09-02']['valuation_key'],raw_points['2026-09-04']['valuation_key'])
        self.assertTrue(combined['history']['cohort']['current_shown_separately'])
        self.assertEqual(combined['history']['cohort']['end_date'], '2026-09-04')
        self.assertEqual(points['2026-09-04']['net_worth'], combined['summary']['net_worth'])
        self.assertIsNone(points['2026-09-03']['net_worth'])
        self.assertIsNone(combined['history']['cohort']['change'])
        self.assertFalse(combined['history']['cohort']['change_comparable'])
        self.assertTrue(any('provider history disagrees' in note for note in combined['warnings']))

    def test_recorded_cutover_uses_utc_receipt_even_for_an_older_or_unknown_balance(self):
        account = {'source': 'snaptrade', 'observations': [
            {'date': '2026-09-17', 'source': 'recorded', 'value': 1000,
             'retrieved_at': '2026-09-19T01:00:00+08:00'},
            {'date': '2026-09-18', 'source': 'recorded', 'value': None,
             'retrieved_at': '2026-09-18T18:00:00Z'},
        ]}
        self.assertEqual(recorded_cutover(account), '2026-09-18')
        account['observations'] = [account['observations'][-1]]
        self.assertEqual(recorded_cutover(account), '2026-09-18', 'Unknown totals still establish collection began')
        account['source'] = 'lunchflow'
        self.assertIsNone(recorded_cutover(account), 'Bank statement/feed carry policy is unchanged')

    def test_recorded_cutover_falls_back_for_legacy_and_skips_invalid_dates(self):
        account = {'source': 'snaptrade', 'observations': [
            {'date': None, 'source': 'recorded', 'retrieved_at': None},
            {'date': 'invalid', 'source': 'recorded', 'retrieved_at': 'invalid'},
        ]}
        self.assertIsNone(recorded_cutover(account))
        account['observations'].append({'date': '2026-09-18', 'source': 'recorded'})
        self.assertEqual(recorded_cutover(account), '2026-09-18')

    def test_reverse_link_keeps_broker_authority_over_current_value_and_later_imports(self):
        source_reconciliation.remove_link(self.conn, 'statement:archive')
        accounts, _ = wealth.sources(self.conn, today=date(2026, 9, 4))
        source_reconciliation.set_link(self.conn, source_id='connected:live', canonical_id='statement:archive',
            evidence_kind='owner_confirmed', evidence_digest='b' * 64, accounts=accounts)
        self.conn.execute('''INSERT INTO statements VALUES(2,'archive','2026-09-03','2026-09-06',NULL,NULL,1,
            '{"parser":"moomoo","base_currency":"GBP","net_asset_value":9000,"native_reconciled":true}')''')
        self.rows.extend({**self.rows[0], 'date': f'2026-09-0{day}', 'value': 9000 + day} for day in (4, 5, 6))
        before = [tuple(row) for row in self.conn.execute('SELECT * FROM statements ORDER BY id')]
        data = wealth.bundle(self.conn, account_ids=['connected:live'], today=date(2026, 9, 7), range_name='1m')
        self.assertEqual(data['selected_account_ids'], ['statement:archive'])
        canonical = next(row for row in data['accounts'] if row['id'] == 'statement:archive')
        self.assertEqual(canonical['recorded_only_from'], '2026-09-04')
        self.assertEqual(canonical['value'], 1200)
        self.assertEqual(canonical['as_of'], '2026-09-04')
        self.assertTrue(canonical['stale'], 'Broker history keeps the broker freshness rule under a statement alias')
        self.assertEqual(data['summary']['net_worth'], 1200)
        self.assertEqual(data['points'][-1]['net_worth'], 1200)
        self.assertEqual(data['history']['cohort']['end_value'], 1200)
        self.assertEqual(data['history']['cohort']['end_date'], '2026-09-04')
        daily = {point['date']: point for point in data['points']}
        self.assertIsNone(daily['2026-09-05']['net_worth'])
        self.assertIsNone(daily['2026-09-06']['net_worth'])
        self.assertEqual([tuple(row) for row in self.conn.execute('SELECT * FROM statements ORDER BY id')], before)
        link = source_reconciliation.list_links(self.conn)[0]
        self.assertEqual((link['source_id'], link['canonical_id']), ('connected:live', 'statement:archive'))

    def test_unlinked_sources_are_never_merged_by_similar_broker_name(self):
        source_reconciliation.remove_link(self.conn,'statement:archive')
        data = wealth.bundle(self.conn, account_ids=['connected:live'],today=date(2026,9,4))
        self.assertFalse(any(point['reconstructed_accounts'] for point in data['history']['cohort']['points']))
        self.assertEqual(data['summary']['net_worth'],1200)

    def test_historical_daily_fx_and_actual_range_are_applied(self):
        self.cached['accounts'][0].update(currency='USD', total_value=2000)
        for day,rate in [(1,.5),(2,.55),(4,.6)]:
            self.conn.execute('INSERT INTO reference_fx VALUES(?,?,?,NULL,?,?,NULL)',
                (f'2026-09-0{day}','USD',rate,f'2026-09-0{day}',f'2026-09-0{day}'))
        data = wealth.bundle(self.conn,currency_code='USD',range_name='1w',today=date(2026,9,4))
        points = {point['date']:point for point in data['history']['cohort']['points']}
        self.assertEqual(points['2026-09-01']['net_worth'],2000)
        self.assertEqual(points['2026-09-02']['net_worth'],2000)
        self.assertEqual(data['summary']['net_worth'],2000)
        self.assertEqual(data['range'],'1w')
        self.assertEqual(wealth.bundle(self.conn,range_name='ytd',today=date(2026,9,4))['range'],'ytd')

    def test_provider_conflict_detection_is_independent_of_selected_range_and_cutoff(self):
        # The evidence of an invalid provider series precedes the visible week.
        # Zooming must not silently re-admit that same series as trustworthy.
        self.cached['accounts'][0]['holdings_synced_at'] = '2026-09-18'
        self.cached['accounts'][0]['total_value_as_of'] = '2026-09-18'
        self.cached['accounts'][0]['history']['data'].append(
            {'date':'2026-09-15', 'total_value':600, 'currency':'GBP'})
        def replay(*args, cutoff=None, **kwargs):
            rows = self.rows if cutoff is None else [row for row in self.rows if row['date'] >= str(cutoff)]
            return {'sources': {'statement:archive': {'observations': rows, 'coverage': {'valued_days':len(rows)}, 'issues':[]}}}
        with patch('app.investment_history.reconstruct', side_effect=replay):
            wide = wealth.bundle(self.conn, account_ids=['connected:live'], range_name='all', today=date(2026,9,18))
            narrow = wealth.bundle(self.conn, account_ids=['connected:live'], range_name='1w', today=date(2026,9,18))
            projections = []
            for cutoff in (None, date(2026,9,11)):
                accounts, _ = wealth.sources(self.conn, today=date(2026,9,18))
                add_history(self.conn, accounts, self.cached, wealth.Rates(self.conn),
                            today=date(2026,9,18), cutoff=cutoff, selected_ids=['connected:live'])
                projections.append(next(account for account in accounts if account['id'] == 'connected:live'))
        wide_account = next(account for account in wide['accounts'] if account['id'] == 'connected:live')
        narrow_account = next(account for account in narrow['accounts'] if account['id'] == 'connected:live')
        self.assertEqual(wide_account['history_provider_conflict'], narrow_account['history_provider_conflict'])
        self.assertEqual(narrow_account['history_provider_conflict']['first_date'], '2026-09-01')
        self.assertEqual(projections[0]['history_observations'], projections[1]['history_observations'])
        self.assertFalse(any(point['source'] == 'snaptrade_estimate' for point in projections[1]['history_observations']))
        self.assertEqual(narrow['summary']['net_worth'],1200)

    def test_null_reconstruction_keeps_supported_statement_anchor_and_unknown_gaps(self):
        # A missing stock price is not evidence that a dated statement NAV is
        # unknown. Retain the anchor while preserving the missing following day.
        self.rows[1].update(value=None, reconciled=False)
        self.rows.append({**self.rows[1], 'date':'2026-09-03'})
        accounts, _ = wealth.sources(self.conn, today=date(2026,9,4))
        original = [dict(point) for point in next(account for account in accounts if account['id'] == 'connected:live')['observations']]
        add_history(self.conn, accounts, self.cached, wealth.Rates(self.conn), today=date(2026,9,4))
        account = next(account for account in accounts if account['id'] == 'connected:live')
        points = {point['date']:point for point in account['history_observations']}
        self.assertEqual(points['2026-09-02']['value'],1100)
        self.assertEqual(points['2026-09-02']['source'],'statement')
        self.assertIsNone(points['2026-09-03']['value'])
        self.assertEqual(account['observations'],original)
        result = wealth.bundle(self.conn, account_ids=['connected:live'], today=date(2026,9,4))
        values = {point['date']:point['net_worth'] for point in result['history']['cohort']['points']}
        self.assertEqual(values['2026-09-02'],1100)
        self.assertIsNone(values['2026-09-03'])
        self.assertEqual(result['summary']['net_worth'],1200)

    def test_monthly_overlap_does_not_replace_dailys_printed_nav_anchor(self):
        self.conn.execute('''INSERT INTO statements VALUES(2,'archive','2026-09-03','2026-09-03',NULL,NULL,1,
            '{"parser":"moomoo","statement_type":"daily","base_currency":"GBP","net_asset_value":1150,
              "native_reconciled":true}')''')
        self.rows.append({'date':'2026-09-03','value':1190,'currency':'GBP','source':'reconstructed',
                          'valuation_basis':'daily_holdings','estimated':True,'reconciled':True,
                          'source_statement_type':'monthly','overlaps_daily_reports':True})
        result = wealth.bundle(self.conn,account_ids=['connected:live'],today=date(2026,9,4))
        self.assertEqual(result['summary']['net_worth'],1200)
        self.assertEqual(next(point for point in result['history']['investment_history']['points']
                              if point['date']=='2026-09-03')['net_worth'],1150)

    def test_investment_statement_nav_never_carries_into_unreported_days(self):
        # Missing verified market prices leave the printed NAV usable on its
        # own date; that does not establish balances for the following gap.
        self.rows[1].update(value=None, reconciled=False)
        result = wealth.bundle(self.conn, account_ids=['connected:live'], today=date(2026, 9, 4))
        points = {point['date']: point for point in result['history']['cohort']['points']}
        self.assertEqual(points['2026-09-02']['net_worth'], 1100)
        self.assertIsNone(points['2026-09-03']['net_worth'])
        self.assertEqual(points['2026-09-04']['net_worth'], 1200)
        self.assertFalse(result['history']['cohort']['change_comparable'])
        # The same gap must remain visible without any reconstructed dates
        # contributing to the chart grid.
        self.rows.clear()
        result = wealth.bundle(self.conn, account_ids=['connected:live'], today=date(2026, 9, 4))
        points = {point['date']: point for point in result['history']['cohort']['points']}
        self.assertEqual(points['2026-09-02']['net_worth'], 1100)
        self.assertIsNone(points['2026-09-03']['net_worth'])
        self.assertEqual(points['2026-09-04']['net_worth'], 1200)

    def test_daily_printed_opening_nav_adds_exact_anchor_without_overriding_live_cutoff(self):
        self.conn.execute('''INSERT INTO statements VALUES(2,'archive','2026-09-04','2026-09-06',NULL,NULL,1,
            '{"parser":"moomoo","statement_type":"daily","base_currency":"GBP","net_asset_value":9000,
              "previous_snapshot_date":"2026-09-03","opening_net_asset_value":1150,"native_reconciled":true}')''')
        self.conn.execute('''INSERT INTO statements VALUES(3,'archive','2026-09-07','2026-09-07',NULL,NULL,1,
            '{"parser":"moomoo","statement_type":"daily","base_currency":"GBP","net_asset_value":9100,
              "previous_snapshot_date":"2026-09-06","opening_net_asset_value":9000,"native_reconciled":true}')''')
        before = [tuple(row) for row in self.conn.execute('SELECT * FROM statements ORDER BY id')]
        result = wealth.bundle(self.conn, account_ids=['connected:live'], today=date(2026, 9, 7))
        points = {point['date']: point for point in result['history']['cohort']['points']}
        self.assertEqual(points['2026-09-03']['net_worth'], 1150)
        self.assertEqual(points['2026-09-03']['valuation_basis'], 'recorded_balance')
        self.assertEqual(points['2026-09-04']['net_worth'], 1200)
        self.assertEqual(result['history']['cohort']['end_date'], '2026-09-04')
        self.assertEqual(result['summary']['net_worth'], 1200)
        self.assertEqual([tuple(row) for row in self.conn.execute('SELECT * FROM statements ORDER BY id')], before)
        self.assertFalse(result['history']['cohort']['change_comparable'])

    def project_provider(self, points, *, selected=True):
        original = [dict(point) for point in points]
        account = {'id':'connected:history', 'name':'Example portfolio', 'kind':'investment', 'observations':points}
        notes = add_history(self.conn, [account], {}, wealth.Rates(self.conn), today=date(2026,9,18),
                            selected_ids=['connected:history'] if selected else [])
        self.assertEqual(account['observations'],original)
        self.assertEqual([(point['date'],point['value']) for point in account['history_observations']],
                         [(point['date'],point['value']) for point in original])
        return account, notes

    def provider_point(self, day, value, source='snaptrade_estimate', code='GBP'):
        return {'date':day, 'value':value, 'currency':code, 'source':source,
                'estimated':source != 'recorded', 'reconciled':True}

    def test_provider_discontinuity_preserves_values_and_segments_later_observations(self):
        points = [self.provider_point('2026-09-01',1000), self.provider_point('2026-09-03',3000),
                  self.provider_point('2026-09-04',3100,'recorded'), self.provider_point('2026-09-05',1000)]
        account, notes = self.project_provider(points)
        self.assertEqual(account['history_discontinuities'],2)
        self.assertEqual([point['comparison_segment'] for point in account['history_observations']],[0,1,1,2])
        self.assertEqual([point['source_break'] for point in account['history_observations']],[False,True,False,True])
        self.assertTrue(any('cash flows or incomplete provider history' in note for note in notes))
        _, hidden_notes = self.project_provider(points,selected=False)
        self.assertEqual(hidden_notes,[])

    def test_discontinuity_checks_use_dated_gbp_values_and_require_both_materiality_thresholds(self):
        for day in ('2026-09-01','2026-09-02'):
            self.conn.execute('INSERT INTO reference_fx VALUES(?,?,?,NULL,?,?,NULL)',(day,'USD',.2,day,day))
        examples = [
            [self.provider_point('2026-09-01',1000,code='USD'), self.provider_point('2026-09-02',3000,code='USD')],
            [self.provider_point('2026-09-01',1000), self.provider_point('2026-09-02',2000)],
            [self.provider_point('2026-09-01',10000), self.provider_point('2026-09-02',14000)],
        ]
        for points in examples:
            with self.subTest(points=points):
                account, notes = self.project_provider(points)
                self.assertEqual(account['history_discontinuities'],0)
                self.assertEqual(notes,[])

    def test_unknown_values_long_gaps_and_nonprovider_comparisons_do_not_trigger(self):
        examples = [
            [self.provider_point('2026-09-01',1000),self.provider_point('2026-09-09',4000)],
            [self.provider_point('2026-09-01',None),self.provider_point('2026-09-02',4000)],
            [self.provider_point('2026-09-01',1000,'recorded'),self.provider_point('2026-09-02',4000,'recorded')],
            [self.provider_point('2026-09-01',1000,'reconstructed'),self.provider_point('2026-09-02',4000)],
        ]
        for points in examples:
            with self.subTest(points=points):
                account, notes = self.project_provider(points)
                self.assertEqual(account['history_discontinuities'],0)
                self.assertEqual(notes,[])

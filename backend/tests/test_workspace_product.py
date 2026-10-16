"""Regression checks for opting into product settings without rewriting history."""
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import analytics, db, main, planning, workspace, currency_reporting, account_settings, wealth
from app.categorize import apply_categorization
from app.lunchflow_store import ensure_schema as banking_schema


class WorkspaceProductTests(unittest.TestCase):
    def setUp(self):
        self.folder = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.folder) / 'test.db'
        self.enterContext(patch.object(db, 'DB_PATH', self.path))
        self.enterContext(patch.object(main, 'DB_PATH', self.path))
        self.enterContext(patch.dict('os.environ', {'FINANCE_AUTH_ENABLED': 'false', 'FINANCE_DEPLOYMENT': 'local'}))
        self.conn = db.init_db(self.path)
        self.addCleanup(self.conn.close)
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('a','Example','current','Main')")
        self.conn.commit()

    def txn(self, amount, category='Groceries', currency=None, day='2026-08-10', key=None):
        ident = self.conn.execute('''INSERT INTO transactions(account_id,txn_date,description,merchant,amount,category,category_source,classification,dedupe_key,currency,meta)
            VALUES('a',?,'Synthetic purchase','Shop',?,?,'auto','{}',?,?,?)''',
            (day, amount, category, key, currency, json.dumps({'source_category': category}))).lastrowid
        self.conn.commit()
        return ident

    def cover(self):
        self.conn.execute("INSERT INTO statements(account_id,source_file,period_start,period_end,reconciled) VALUES('a','synthetic','2026-08-01','2026-08-31',1)")
        self.conn.commit()

    def native_bank(self):
        banking_schema(self.conn)
        self.conn.execute("UPDATE accounts SET currency='EUR' WHERE id='a'")
        self.conn.execute("INSERT INTO bank_connections(id,name,institution_name,currency,account_id,raw,first_seen_at,last_seen_at) VALUES('bank','Bank','Example','EUR','a','{}','2026-08-10','2026-08-10')")
        ident = self.conn.execute("INSERT INTO bank_transactions(connection_id,provider_id,txn_date,description,amount,currency,is_pending,status,raw,first_seen_at,last_seen_at) VALUES('bank','purchase','2026-08-10','Synthetic purchase',-100,'EUR',0,'native','{}','2026-08-10','2026-08-10')").lastrowid
        self.conn.commit()
        return ident

    def test_newer_schema_is_refused_before_applying_migrations(self):
        self.conn.execute(f'PRAGMA user_version={db.SCHEMA_VERSION+1}')
        before = list(self.conn.execute('SELECT name,sql FROM sqlite_master ORDER BY name'))
        with self.assertRaisesRegex(RuntimeError, 'newer application release'):
            db.init_db(self.path)
        self.assertEqual(list(self.conn.execute('SELECT name,sql FROM sqlite_master ORDER BY name')), before)
        self.assertEqual(self.conn.execute('PRAGMA user_version').fetchone()[0], db.SCHEMA_VERSION+1)

    def test_startup_does_not_replace_empty_existing_database_with_a_new_household(self):
        empty = Path(self.folder) / 'empty.db'
        empty.touch()
        with patch.object(main, 'DB_PATH', empty), self.assertRaisesRegex(RuntimeError, 'not an initialized financial ledger'):
            with TestClient(main.app):
                pass
        self.assertEqual(empty.read_bytes(), b'')

    def test_default_income_category_preserves_refund_description_precedence(self):
        ident = self.txn(100, category='Income')
        self.conn.execute("UPDATE transactions SET description='Interest reversal refund' WHERE id=?", (ident,))
        self.conn.commit()
        month = analytics.monthly_cashflow(self.conn)[0]
        self.assertEqual(month['income'], 0)
        self.assertEqual(month['refunds'], 100)

    def test_currency_report_counts_printed_fee_without_exchange_principal(self):
        ident = self.txn(-100, 'Transfers & payments')
        self.conn.execute("UPDATE transactions SET spending_treatment='self_transfer',meta=? WHERE id=?", (json.dumps({'currency_exchange': True, 'exchange_fee_gbp': 2}), ident))
        self.conn.commit()
        result = currency_reporting.cashflow_report(self.conn)
        self.assertEqual(result['totals']['spending'], 2)
        self.assertEqual(result['categories'], [{'category':'Fees & interest','spending':2}])
        self.assertEqual(result['events'][0]['native_amount'], -100)
        self.assertEqual(result['events'][0]['reporting_amount'], -2)
        self.assertEqual(analytics.cashflow_rows(self.conn)[0]['category_semantic'], 'Fees & interest')

    def test_reporting_coverage_includes_accounts_excluded_only_from_planning(self):
        self.txn(-10)
        self.cover()
        self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('other','Other','current','Business')")
        account_settings.save_settings(self.conn, 'other', {'include_in_planning': False})
        self.conn.commit()
        self.assertFalse(currency_reporting.cashflow_report(self.conn)['coverage']['2026-08']['complete'])

    def test_fx_rejects_unsafe_scale_and_never_serializes_nonfinite_totals(self):
        for rate in (1e-300, 1e300):
            response = self.client.post('/api/workspace/exchange-rates', json={'currency':'EUR','observed_date':'2026-08-10','rate_to_gbp':rate,'evidence':'Synthetic reference'})
            self.assertEqual(response.status_code, 422)
        self.txn(-100, currency='EUR')
        self.conn.execute("INSERT INTO workspace_fx_rates(currency,observed_date,rate_to_gbp,evidence) VALUES('EUR','2026-08-10',1e308,'Synthetic corrupted reference')")
        self.conn.commit()
        result = self.client.get('/api/workspace/reports/cashflow?currency=GBP')
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()['partial'])
        self.assertEqual(result.json()['missing_fx_count'], 1)

    def test_native_bank_uses_saved_rules_and_review_preserves_evidence(self):
        ident = self.native_bank()
        self.conn.execute("INSERT INTO rules(pattern,category) VALUES('Synthetic purchase','Shopping')")
        self.conn.commit()
        original = tuple(self.conn.execute('SELECT * FROM bank_transactions WHERE id=?',(ident,)).fetchone())
        event = currency_reporting.cashflow_report(self.conn, currency='EUR')['events'][0]
        self.assertEqual(event['category'], 'Shopping')
        body = {'evidence_version':event['evidence_version'],'category':'Groceries','spending_treatment':'normal','excluded':True}
        response = self.client.put(f'/api/workspace/native-transactions/{ident}/review',json=body)
        self.assertEqual(response.status_code, 200, response.text)
        report = currency_reporting.cashflow_report(self.conn, currency='EUR')
        self.assertEqual(report['totals']['spending'], 0)
        self.assertEqual(report['events'][0]['category'], 'Groceries')
        self.assertEqual(report['events'][0]['omission_reason'], 'User exclusions')
        self.assertEqual(tuple(self.conn.execute('SELECT * FROM bank_transactions WHERE id=?',(ident,)).fetchone()), original)
        self.assertEqual(self.client.delete(f'/api/workspace/native-transactions/{ident}/review').status_code,200)
        self.assertEqual(currency_reporting.cashflow_report(self.conn,currency='EUR')['totals']['spending'],100)

    def test_native_review_needs_new_confirmation_after_provider_evidence_changes(self):
        ident = self.native_bank()
        event = currency_reporting.cashflow_report(self.conn, currency='EUR')['events'][0]
        body = {'evidence_version':event['evidence_version'],'category':'Groceries'}
        self.assertEqual(self.client.put(f'/api/workspace/native-transactions/{ident}/review',json=body).status_code,200)
        self.conn.execute('UPDATE bank_transactions SET amount=-200 WHERE id=?',(ident,))
        self.conn.commit()
        self.assertEqual(self.client.put(f'/api/workspace/native-transactions/{ident}/review',json=body).status_code,409)
        report = currency_reporting.cashflow_report(self.conn,currency='EUR')
        self.assertEqual(report['stale_review_count'],1)
        self.assertEqual(report['totals']['spending'],0)
        self.assertTrue(report['partial'])

    def test_native_overlap_needs_evidence_and_distinct_or_duplicate_decision(self):
        ident = self.native_bank()
        self.txn(-100,currency='EUR')
        report = currency_reporting.cashflow_report(self.conn,currency='EUR')
        event = next(item for item in report['events'] if item['event_id'].startswith('bank:'))
        body = {'evidence_version':event['evidence_version'],'category':'Groceries','overlap_decision':'distinct'}
        path = f'/api/workspace/native-transactions/{ident}/review'
        self.assertEqual(self.client.put(path,json=body).status_code,422)
        body['note'] = 'Different receipt identifiers checked'
        self.assertEqual(self.client.put(path,json=body).status_code,200)
        report = currency_reporting.cashflow_report(self.conn,currency='EUR')
        self.assertEqual(report['totals']['spending'],200)
        self.assertFalse(report['partial'])
        body['overlap_decision'] = 'duplicate'
        self.assertEqual(self.client.put(path,json=body).status_code,200)
        report = currency_reporting.cashflow_report(self.conn,currency='EUR')
        self.assertEqual(report['totals']['spending'],100)
        self.assertFalse(report['partial'])

    def test_defaults_and_workspace_identity_survive_initialization(self):
        self.assertEqual(workspace.preferences(self.conn), workspace.DEFAULTS)
        ident = self.conn.execute('SELECT id FROM workspace').fetchone()[0]
        self.conn.close()
        self.conn = db.init_db(self.path)
        self.addCleanup(self.conn.close)
        self.assertEqual(self.conn.execute('SELECT id FROM workspace').fetchone()[0], ident)
        self.assertEqual(analytics.monthly_cashflow(self.conn), [])

    def test_preferences_validate_without_changing_source_values(self):
        ident = self.txn(-10)
        before = tuple(self.conn.execute('SELECT * FROM transactions WHERE id=?', (ident,)).fetchone())
        result = self.client.patch('/api/workspace/preferences', json={'reporting_currency': 'EUR', 'locale': 'fr-FR', 'timezone': 'Europe/Paris'})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['reporting_currency'], 'EUR')
        for values in ({'reporting_currency': 'XYZ'}, {'timezone': '../etc/passwd'}, {'locale': '<script>'}, {'reporting_currency': None}, {'database_path': '/other.db'}):
            self.assertEqual(self.client.patch('/api/workspace/preferences', json=values).status_code, 422)
        self.assertEqual(tuple(self.conn.execute('SELECT * FROM transactions WHERE id=?', (ident,)).fetchone()), before)

    def test_category_rename_keeps_identity_and_transfer_semantics(self):
        ident = self.txn(-100, 'Transfers & payments', key='source')
        category = workspace.category_id('Transfers & payments')
        result = self.client.patch('/api/workspace/categories/' + category, json={'name': 'Between my accounts'})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['id'], category)
        self.assertEqual(self.conn.execute('SELECT category FROM transactions WHERE id=?', (ident,)).fetchone()[0], 'Between my accounts')
        self.assertEqual(analytics.cashflow_rows(self.conn), [])
        apply_categorization(self.conn)
        self.conn.commit()
        self.assertEqual(self.conn.execute('SELECT category FROM transactions WHERE id=?', (ident,)).fetchone()[0], 'Between my accounts')
        self.assertEqual(self.conn.execute('SELECT amount FROM transactions WHERE id=?', (ident,)).fetchone()[0], -100)
        self.assertEqual(self.client.post('/api/workspace/categories', json={'name': 'Transfers & payments'}).status_code, 409)

    def test_category_archival_hides_choice_not_history(self):
        self.txn(-10)
        category = workspace.category_id('Groceries')
        result = self.client.patch('/api/workspace/categories/' + category, json={'archived': True})
        self.assertEqual(result.status_code, 200)
        self.assertNotIn('Groceries', self.client.get('/api/category-options').json())
        self.assertEqual(analytics.cashflow_rows(self.conn)[0]['category'], 'Groceries')

    def test_custom_treatment_is_separate_from_label(self):
        created = self.client.post('/api/workspace/categories', json={'name': 'My movements', 'treatment': 'transfer'})
        self.assertEqual(created.status_code, 201)
        self.txn(-100, 'My movements')
        self.assertEqual(analytics.cashflow_rows(self.conn), [])
        response = self.client.patch('/api/workspace/categories/' + created.json()['id'], json={'treatment': 'normal'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(analytics.cashflow_rows(self.conn)[0]['amount'], -100)

    def test_builtin_income_can_explicitly_become_a_spending_category(self):
        self.txn(50, category='Income')
        self.assertEqual(analytics.monthly_cashflow(self.conn)[0]['income'], 50)
        response = self.client.patch('/api/workspace/categories/' + workspace.category_id('Income'), json={'treatment':'normal'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(analytics.monthly_cashflow(self.conn)[0]['income'], 0)
        self.assertEqual(analytics.monthly_cashflow(self.conn)[0]['refunds'], 50)

    def test_native_currency_never_enters_legacy_pound_totals(self):
        self.txn(-10)
        self.txn(-100, currency='EUR')
        self.assertEqual(analytics.monthly_cashflow(self.conn)[0]['spend'], 10)
        response = self.client.get('/api/transactions').json()
        self.assertEqual(response['total'], 1)
        self.assertEqual(response['sum_out'], -10)
        report = currency_reporting.cashflow_report(self.conn, currency='EUR')
        self.assertEqual(report['totals']['spending'], 100)
        self.assertTrue(report['partial'])
        self.assertEqual(report['missing_fx_count'], 1)
        self.assertEqual(report['native_totals']['GBP']['spending'], 10)

    def test_reference_currency_uses_dated_evidence_without_future_or_stale_rates(self):
        self.txn(-100, currency='EUR')
        self.conn.execute("INSERT INTO workspace_fx_rates(currency,observed_date,rate_to_gbp,evidence) VALUES('EUR','2026-08-10',0.85,'Synthetic source')")
        self.conn.commit()
        report = currency_reporting.cashflow_report(self.conn, currency='GBP')
        self.assertEqual(report['totals']['spending'], 85)
        self.assertFalse(report['partial'])
        self.assertEqual(report['events'][0]['fx']['original_reference']['source'], 'User-supplied reference')
        self.txn(-100, currency='EUR', day='2026-08-09')
        self.txn(-100, currency='EUR', day='2026-08-18')
        report = currency_reporting.cashflow_report(self.conn, currency='GBP')
        self.assertEqual(report['missing_fx_count'], 2)
        self.assertEqual(report['totals']['spending'], 85)

    def test_monthly_buckets_do_not_repeat_running_totals(self):
        self.txn(-10, day='2026-07-10')
        self.txn(-20, day='2026-08-10')
        result = currency_reporting.cashflow_report(self.conn)
        self.assertEqual([r['spending'] for r in result['monthly']], [10, 20])
        self.assertEqual(result['totals']['spending'], 30)

    def test_native_report_keeps_three_decimal_currency_precision(self):
        self.txn(-1.003, currency='KWD')
        result = currency_reporting.cashflow_report(self.conn, currency='KWD')
        self.assertEqual(result['totals']['spending'], 1.003)
        self.assertEqual(result['events'][0]['reporting_amount'], -1.003)
        self.assertEqual(result['native_totals']['KWD']['spending'], 1.003)

    def test_report_rejects_unknown_or_excessive_account_selection(self):
        self.assertEqual(self.client.get('/api/workspace/reports/cashflow?account=missing').status_code, 422)
        with self.assertRaises(ValueError):
            currency_reporting.cashflow_report(self.conn, account=['a'] * 101)

    def test_account_share_and_exclusion_change_reports_without_editing_transactions(self):
        ident = self.txn(-100)
        account_settings.save_settings(self.conn, 'a', {'ownership_share': .5})
        self.conn.commit()
        self.assertEqual(analytics.cashflow_rows(self.conn)[0]['amount'], -50)
        self.assertEqual(currency_reporting.cashflow_report(self.conn)['totals']['spending'], 50)
        self.assertEqual(self.conn.execute('SELECT amount FROM transactions WHERE id=?', (ident,)).fetchone()[0], -100)
        account_settings.save_settings(self.conn, 'a', {'include_in_reports': False})
        self.conn.commit()
        self.assertEqual(analytics.cashflow_rows(self.conn), [])
        self.assertEqual(currency_reporting.cashflow_report(self.conn)['totals']['spending'], 0)

    def test_coverage_respects_new_account_opening_and_explicit_gaps(self):
        self.txn(-10)
        self.cover()
        self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('new','Another','savings','New savings')")
        account_settings.save_settings(self.conn, 'new', {'opened_on': '2026-09-01'})
        self.conn.commit()
        self.assertEqual(planning.covered_baseline(self.conn, today=date(2026,9,21))['months'], 1)
        response = self.client.post('/api/workspace/accounts/a/coverage', json={'period_start':'2026-08-10','period_end':'2026-08-11','complete':False,'evidence':'A confirmed missing page'})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(planning.covered_baseline(self.conn, today=date(2026,9,21))['months'], 0)
        self.assertEqual(self.client.delete('/api/workspace/accounts/a/coverage/' + str(response.json()['id'])).status_code, 200)
        self.assertEqual(planning.covered_baseline(self.conn, today=date(2026,9,21))['months'], 1)

    def test_api_only_coverage_requires_explicit_evidence(self):
        self.txn(-10)
        self.assertEqual(planning.covered_baseline(self.conn, today=date(2026,9,21))['months'], 0)
        result = self.client.post('/api/workspace/accounts/a/coverage', json={'period_start':'2026-08-01','period_end':'2026-08-31','complete':True,'evidence':'Checked complete provider export'})
        self.assertEqual(result.status_code, 201, result.text)
        baseline = planning.covered_baseline(self.conn, today=date(2026,9,21))
        self.assertEqual(baseline['months'], 1)
        self.assertEqual(baseline['coverage_sources'], ['user_confirmed'])

    def test_excluding_account_from_planning_excludes_its_spending(self):
        self.txn(-10)
        self.cover()
        self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('other','Other','current','Business')")
        self.conn.execute("INSERT INTO transactions(account_id,txn_date,description,amount,category) VALUES('other','2026-08-10','Business',-1000,'Groceries')")
        account_settings.save_settings(self.conn, 'other', {'include_in_planning': False})
        self.conn.commit()
        baseline = planning.covered_baseline(self.conn, today=date(2026,9,21))
        self.assertEqual(baseline['months'], 1)
        self.assertEqual(baseline['avg_spending'], 10)

    def test_native_bank_overlap_is_withheld_and_pending_never_reported(self):
        banking_schema(self.conn)
        self.conn.execute("UPDATE accounts SET currency='EUR' WHERE id='a'")
        self.conn.execute("INSERT INTO bank_connections(id,name,institution_name,currency,account_id,raw,first_seen_at,last_seen_at) VALUES('bank','Bank','Example','EUR','a','{}','2026-08-10','2026-08-10')")
        for ident, pending in [('booked',0), ('pending',1)]:
            self.conn.execute("INSERT INTO bank_transactions(connection_id,provider_id,txn_date,amount,currency,is_pending,status,raw,first_seen_at,last_seen_at) VALUES('bank',?,'2026-08-10',-100,'EUR',?,'native','{}','2026-08-10','2026-08-10')", (ident, pending))
        self.conn.commit()
        self.assertEqual(currency_reporting.cashflow_report(self.conn, currency='EUR')['totals']['spending'], 100)
        self.txn(-100, currency='EUR')
        result = currency_reporting.cashflow_report(self.conn, currency='EUR')
        self.assertEqual(result['totals']['spending'], 100)
        self.assertEqual(result['possible_overlap_count'], 1)

    def test_manual_balances_feed_net_worth_without_inventing_transactions(self):
        created = self.client.post('/api/workspace/accounts', json={'display_name': 'Cash jar', 'account_type': 'cash', 'currency': 'GBP'})
        self.assertEqual(created.status_code, 201, created.text)
        ident = created.json()['id']
        result = self.client.post(f'/api/workspace/accounts/{ident}/balances', json={'amount':123,'currency':'GBP','as_of':'2026-08-31'})
        self.assertEqual(result.status_code, 201, result.text)
        snapshot = next(a for a in planning.account_snapshot(self.conn, date(2026,9,21)) if a['id'] == ident)
        self.assertEqual(snapshot['balance'], 123)
        self.assertEqual(snapshot['balance_source'], 'manual')
        sources, _ = wealth.sources(self.conn, path=self.path, today=date(2026,9,21))
        observed = next(a for a in sources if a['id'] == 'statement:' + ident)
        self.assertEqual(observed['observations'][-1]['value'], 123)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions WHERE account_id=?',(ident,)).fetchone()[0],0)

    def test_new_routes_remain_authenticated(self):
        with patch.dict('os.environ', {'FINANCE_AUTH_ENABLED':'true', 'FINANCE_AUTH_DB_PATH': str(Path(self.folder)/'auth.db')}):
            for path in ('/api/workspace/preferences','/api/workspace/categories','/api/workspace/accounts','/api/workspace/reports/cashflow','/api/connections'):
                self.assertEqual(self.client.get(path).status_code, 401)


if __name__ == '__main__':
    unittest.main()

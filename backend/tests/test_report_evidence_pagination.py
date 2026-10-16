"""Complete evidence review and bounded catalog reads on isolated synthetic data."""
import unittest
from contextlib import closing
from unittest.mock import patch

from app import currency_reporting, product_imports, db
from tests import test_workspace_product as workspace_fixture
from tests import test_product_accounts_connections_imports as import_fixture


class ReportEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = workspace_fixture.WorkspaceProductTests('test_defaults_and_workspace_identity_survive_initialization')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.conn, self.client = self.fixture.conn, self.fixture.client
        self.first_id = self.fixture.native_bank()
        with self.conn:
            self.conn.executemany('''INSERT INTO bank_transactions
                (connection_id,provider_id,txn_date,description,amount,currency,is_pending,status,raw,first_seen_at,last_seen_at)
                VALUES('bank',?,'2026-08-10','Synthetic purchase',-1,'EUR',0,'native','{}','2026-08-10','2026-08-10')''',
                [(f'synthetic-{number}',) for number in range(204)])

    def test_all_same_day_events_and_omissions_are_reachable_and_reviewable(self):
        with self.conn:
            self.conn.execute("UPDATE bank_transactions SET status='review',review_reason='Synthetic source conflict' WHERE id=?", (self.first_id,))
        url = '/api/workspace/reports/cashflow?currency=EUR&date_from=2026-08-10&date_to=2026-08-10&account=a'
        first = self.client.get(url).json()
        second = self.client.get(url + '&offset=' + str(first['next_offset']) + '&revision=' + first['report_revision']).json()
        self.assertEqual((first['event_count'], len(first['events']), len(second['events'])), (205, 200, 5))
        self.assertIsNone(second['next_offset'])
        self.assertEqual(first['totals'], second['totals'])
        events = first['events'] + second['events']
        self.assertEqual(len({item['event_id'] for item in events}), 205)
        self.assertEqual({item['event_id'] for item in events}, {f'bank:{row[0]}' for row in self.conn.execute('SELECT id FROM bank_transactions')})
        self.assertEqual(events, sorted(events, key=lambda item: (item['txn_date'], item['event_id']), reverse=True))
        self.assertTrue(next(item for item in events if item['event_id'] == f'bank:{self.first_id}')['omission_reason'])
        self.assertEqual(self.client.get(url).json()['events'], first['events'])
        review = next(item for item in second['events'] if not item['source_review'])
        response = self.client.put('/api/workspace/native-transactions/' + review['event_id'][5:] + '/review',
            json={'evidence_version': review['evidence_version'], 'category': 'Groceries', 'excluded': True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get(url + '&offset=200&revision=' + first['report_revision']).status_code, 409)
        updated_revision = self.client.get(url).json()['report_revision']
        refreshed = self.client.get(url + '&offset=200&revision=' + updated_revision).json()
        self.assertTrue(next(item for item in refreshed['events'] if item['event_id'] == review['event_id'])['reviewed'])
        self.assertEqual(refreshed['totals']['spending'], first['totals']['spending'] - 1)
        self.assertEqual(self.client.get(url + '&offset=-1').status_code, 422)
        self.assertEqual(self.client.get(url + '&offset=100001').status_code, 422)

    def test_revision_rejects_provider_membership_and_evidence_changes_between_pages(self):
        url = '/api/workspace/reports/cashflow?currency=EUR&account=a'
        self.assertEqual(self.client.get(url + '&offset=200').status_code, 409)
        for mutation in (
            "UPDATE bank_transactions SET is_pending=1 WHERE id=1",
            "UPDATE bank_transactions SET is_pending=0 WHERE id=1",
            "UPDATE bank_transactions SET description='Updated synthetic source' WHERE id=2",
            "UPDATE bank_transactions SET amount=-3 WHERE id=3",
        ):
            before = self.client.get(url).json()
            with self.conn:
                self.conn.execute(mutation)
            stale = self.client.get(url + '&offset=200&revision=' + before['report_revision'])
            self.assertEqual(stale.status_code, 409, stale.text)
            self.assertIn('first evidence page', stale.json()['detail'])
            current = self.client.get(url).json()
            self.assertNotEqual(current['report_revision'], before['report_revision'])
            later = self.client.get(url + '&offset=200&revision=' + current['report_revision'])
            self.assertEqual(later.status_code, 200, later.text)
            all_ids = [item['event_id'] for item in current['events'] + later.json()['events']]
            self.assertEqual(len(all_ids), current['event_count'])
            self.assertEqual(len(set(all_ids)), current['event_count'])

    def test_api_report_uses_one_snapshot_when_another_connection_updates_evidence_and_rates(self):
        with self.conn:
            self.conn.execute("INSERT INTO workspace_fx_rates(currency,observed_date,rate_to_gbp,evidence) VALUES('EUR','2026-08-10',0.8,'Synthetic original rate')")
        original = currency_reporting.native_events
        def update_after_event_reads(conn, **kwargs):
            self.assertTrue(conn.in_transaction)
            events = original(conn, **kwargs)
            # This is a separate actual connection committing after the report's
            # evidence reads but before its FX reads; neither query is fabricated.
            with closing(db.connect(self.fixture.path)) as writer, writer:
                writer.execute("UPDATE workspace_fx_rates SET rate_to_gbp=0.9")
                writer.execute("UPDATE bank_transactions SET amount=-1000 WHERE id=?", (self.first_id,))
            return events
        with patch.object(currency_reporting, 'native_events', side_effect=update_after_event_reads):
            response = self.client.get('/api/workspace/reports/cashflow?currency=GBP')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['totals']['spending'], 243.2)
        next_report = self.client.get('/api/workspace/reports/cashflow?currency=GBP').json()
        self.assertEqual(next_report['totals']['spending'], 1083.6)
        self.assertNotEqual(next_report['report_revision'], response.json()['report_revision'])

    def test_catalog_reads_are_bounded_for_large_and_empty_reports_and_renames_refresh(self):
        with self.conn:
            self.conn.executemany("INSERT INTO category_catalog(id,name,semantic_name,treatment,sort_order) VALUES(?,?,?,'normal',100)",
                [(f'synthetic-{i}', f'Synthetic category {i}', f'Synthetic category {i}') for i in range(100)])
            self.conn.executemany('''INSERT INTO transactions(account_id,txn_date,description,amount,category,currency)
                VALUES('a','2026-08-11','Synthetic ledger purchase',-2,'Groceries','EUR')''', [()] * 5000)
            self.conn.execute("INSERT INTO rules(pattern,category) VALUES('Synthetic purchase','Groceries')")
        def measured(**kwargs):
            statements = []
            self.conn.set_trace_callback(statements.append)
            try:
                report = currency_reporting.cashflow_report(self.conn, currency='EUR', **kwargs)
            finally:
                self.conn.set_trace_callback(None)
            catalog_reads = [sql for sql in statements if 'SELECT * FROM category_catalog' in sql or 'SELECT * FROM category_names' in sql]
            self.assertEqual(len(catalog_reads), 2, catalog_reads)
            return report
        report = measured()
        self.assertEqual(report['event_count'], 5205)
        self.assertEqual(report['totals']['spending'], 10304)
        self.assertEqual(report['categories'], [{'category': 'Groceries', 'spending': 10304}])
        self.assertEqual(measured(date_from='2026-08-12')['event_count'], 0)
        with self.conn:
            self.conn.execute("UPDATE category_catalog SET name='Food at home' WHERE name='Groceries'")
        renamed = measured()
        self.assertEqual(renamed['categories'], [{'category': 'Food at home', 'spending': 10304}])
        self.assertTrue(all(event['category'] == 'Food at home' for event in renamed['events']))
        native = currency_reporting.native_events(self.conn)
        self.assertTrue(all(row['category'] == 'Food at home' for row in native if row['source'] == 'bank_feed'))


class CsvEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = import_fixture.ProductTests('test_csv_requires_fresh_preview_and_explicit_overlap_choice')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.conn = self.fixture.conn

    def test_overlap_after_sample_boundary_remains_visible_and_confirmation_is_authoritative(self):
        with self.conn:
            self.conn.execute("INSERT INTO transactions(account_id,txn_date,description,amount) VALUES(?,'2026-08-01','Synthetic prior purchase',-201)", (self.fixture.account['id'],))
        body = self.fixture.csv('date,description,amount\n' + ''.join(f'2026-08-01,Synthetic row {i},-{i}\n' for i in range(1, 202)))
        private = product_imports.prepare(self.conn, body)
        public = product_imports.public_preview(private)
        self.assertEqual((public['row_count'], len(public['rows']), public['review_count']), (201, 200, 1))
        self.assertFalse(any(row['status'] == 'review' for row in public['rows']))
        self.assertEqual([row['row'] for row in public['review_rows']], [202])
        self.assertTrue(public['review_rows'][0]['reason'])
        self.assertFalse(any(key.startswith('_') for row in public['rows'] + public['review_rows'] for key in row))
        with self.assertRaisesRegex(ValueError, 'overlaps'):
            self.fixture.commit(body)
        self.assertEqual(self.fixture.commit(body, overlaps=True)['imported_count'], 201)
        # Public evidence must not weaken the unchanged freshness contract.
        with self.assertRaisesRegex(ValueError, 'changed'):
            with self.conn:
                product_imports.commit_import(self.conn, product_imports.CsvCommit(**body.model_dump(), preview_token=public['preview_token'], confirm_overlaps=True))

    def test_all_flagged_rows_are_returned_even_when_every_row_needs_review(self):
        with self.conn:
            self.conn.execute("INSERT INTO statements(account_id,source_file,period_start,period_end) VALUES(?,'synthetic','2026-08-01','2026-08-31')", (self.fixture.account['id'],))
        body = self.fixture.csv('date,description,amount\n' + ''.join(f'2026-08-01,Synthetic row {i},-{i}\n' for i in range(1, 406)))
        result = product_imports.public_preview(product_imports.prepare(self.conn, body))
        self.assertEqual(result['review_count'], 405)
        self.assertEqual([row['row'] for row in result['review_rows']], list(range(2, 407)))
        self.assertEqual(len(result['rows']), 200)


if __name__ == '__main__':
    unittest.main()

"""Synthetic banking storage tests: preservation, idempotency, reconciliation."""
import json
from pathlib import Path
import tempfile
import unittest

from app.db import init_db
from app import lunchflow_client as client, lunchflow_store as store
from app.lunchflow_scheduler import seconds_until_due


class LunchFlowStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name)/'test.db'
        self.conn = init_db(self.path)
        self.addCleanup(self.conn.close)
        store.ensure_schema(self.conn)
        self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('historic','HSBC','current','Old account')")
        self.conn.execute("INSERT INTO statements(id,account_id,source_file,period_start,period_end,closing_balance,reconciled) VALUES(1,'historic','original.pdf','2026-08-01','2026-08-31',100,1)")
        self.conn.execute("INSERT INTO transactions(id,account_id,statement_id,txn_date,description,merchant,amount,category,category_source,dedupe_key) VALUES(1,'historic',1,'2026-08-29','COFFEE SHOP','Coffee Shop',-12.34,'User choice','user','original-key')")
        self.baseline = dict(self.conn.execute('SELECT * FROM transactions WHERE id=1').fetchone())
        self.accounts = client.list_accounts({'accounts':[{'id':42,'name':'GBP account','institution_name':'HSBC UK','status':'ACTIVE','provider':'test'}],'total':1})
        store.discover_accounts(self.conn,self.accounts,'2026-09-18T10:00:00+00:00')
        self.conn.commit()

    def txn(self,key='tx1',day='2026-08-31',amount=-12.34,description='Coffee Shop',pending=False,currency='GBP'):
        raw = {'id':key,'accountId':42,'date':day,'amount':amount,'currency':currency,'description':description,'merchant':description,'isPending':pending}
        return client.transactions({'transactions':[raw],'total':1},'42')[0]

    def save(self,rows,stamp='2026-09-18T10:10:00+00:00',balance=100,errors=None):
        store.record_account_result(self.conn,'42',{'balance':{'amount':balance,'currency':'GBP'} if balance is not None else None,'transactions':rows,'errors':errors or []},stamp)
        self.conn.commit()

    def map(self):
        store.map_account(self.conn,'42',account_id='historic')
        self.conn.commit()

    def test_overlap_links_same_economic_event_without_rewriting_statement(self):
        self.save([self.txn()])
        self.assertIsNone(self.conn.execute('SELECT account_id FROM bank_connections').fetchone()[0])
        self.map()
        self.assertEqual(dict(self.conn.execute('SELECT * FROM transactions WHERE id=1').fetchone()),self.baseline)
        self.assertEqual(self.conn.execute('SELECT status,transaction_id FROM bank_transactions').fetchone()[:],('matched',1))
        self.save([self.txn()],'2026-09-18T11:00:00+00:00')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0],1)

    def test_new_booked_rows_import_once_pending_never_enters_ledger(self):
        rows = [self.txn('new','2026-09-15',-25,'Brand new merchant'),self.txn('pending','2026-09-17',-7,'Hold',True)]
        self.save(rows)
        self.map()
        self.save(rows,'2026-09-18T11:00:00+00:00')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0],2)
        self.assertEqual(self.conn.execute("SELECT status FROM bank_transactions WHERE provider_id='pending'").fetchone()[0],'pending')
        self.save([rows[0]],'2026-09-18T12:00:00+00:00')
        self.assertEqual(self.conn.execute("SELECT status FROM bank_transactions WHERE provider_id='pending'").fetchone()[0],'expired')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM bank_transactions').fetchone()[0],2)

    def test_same_amount_and_dates_are_not_enough_to_merge(self):
        self.save([self.txn(description='Different merchant')])
        self.map()
        self.assertEqual(self.conn.execute('SELECT status FROM bank_transactions').fetchone()[0],'review')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0],1)
        key=self.conn.execute('SELECT id FROM bank_transactions').fetchone()[0]
        store.resolve_review(self.conn,key,'import')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0],2)

    def test_duplicate_source_occurrences_are_held_and_one_to_one_link_enforced(self):
        self.save([self.txn('first'),self.txn('second')])
        self.map()
        self.assertEqual([r[0] for r in self.conn.execute('SELECT status FROM bank_transactions')],['review','review'])
        ids = [r[0] for r in self.conn.execute('SELECT id FROM bank_transactions')]
        store.resolve_review(self.conn,ids[0],'match',1)
        with self.assertRaises(ValueError):
            store.resolve_review(self.conn,ids[1],'match',1)

    def test_provider_revision_retains_original_and_explicit_ignore_is_durable(self):
        self.save([self.txn()])
        self.map()
        revised=self.txn(amount=-13)
        self.save([revised],'2026-09-18T11:00:00+00:00')
        row=self.conn.execute('SELECT * FROM bank_transactions').fetchone()
        self.assertEqual(row['status'],'review')
        store.resolve_review(self.conn,row['id'],'ignore')
        self.save([revised],'2026-09-18T12:00:00+00:00')
        self.assertEqual(self.conn.execute('SELECT status FROM bank_transactions').fetchone()[0],'ignored')
        self.assertEqual(dict(self.conn.execute('SELECT * FROM transactions WHERE id=1').fetchone()),self.baseline)
        self.assertGreater(self.conn.execute('SELECT COUNT(*) FROM bank_transaction_revisions').fetchone()[0],0)

    def test_failure_preserves_previous_balance_and_rows(self):
        self.save([self.txn()])
        self.map()
        self.save(None,'2026-09-18T11:00:00+00:00',balance=None,errors=['Synthetic provider failure'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM bank_transactions').fetchone()[0],1)
        saved=store.latest_balances(self.conn)['historic']
        self.assertEqual(saved['amount'],100)
        self.assertTrue(saved['stale'])
        self.assertIn('Synthetic',saved['error'])

    def test_native_currency_and_mixed_account_currency_never_enter_gbp_ledger(self):
        self.save([self.txn('foreign','2026-09-15',-100,'Singapore purchase',currency='SGD')])
        self.map()
        self.assertEqual(self.conn.execute('SELECT status FROM bank_transactions').fetchone()[0],'native')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0],1)
        detail=store.detail(self.conn,'42')
        self.assertEqual(detail['totals_by_currency']['SGD']['money_out'],100)

    def test_reverse_sign_and_account_mapping_validation(self):
        self.save([self.txn(amount=12.34)])
        store.map_account(self.conn,'42',account_id='historic',amount_multiplier=-1,balance_multiplier=-1)
        self.assertEqual(self.conn.execute('SELECT status FROM bank_transactions').fetchone()[0],'matched')
        self.assertEqual(store.latest_balances(self.conn)['historic']['amount'],-100)
        with self.assertRaises(ValueError):
            store.map_account(self.conn,'42',account_id='historic',amount_multiplier=1)

    def test_filters_pagination_and_masking(self):
        self.save([self.txn('a','2026-09-10',-1,'100% store'),self.txn('b','2026-09-11',-2,'Second store')])
        self.map()
        result=store.detail(self.conn,'42',q='100%')
        self.assertEqual(result['total'],1)
        self.assertEqual(result['transactions'][0]['description'],'100% store')
        self.assertEqual(store.detail(self.conn,'42',date_from='2026-09-11')['total'],1)
        self.assertNotIn('12345678',store.public_name('GB29 NWBK 601613 12345678'))

    def test_statement_after_feed_matches_posting_date_shift_and_guards_ambiguity(self):
        self.save([self.txn('new','2026-09-15',-22,'Brand new merchant')])
        self.map()
        row=store.match_statement_transaction(self.conn,'historic','2026-09-10',-22,'Brand new merchant')
        self.assertIsNotNone(row)
        with self.assertRaises(ValueError):
            store.match_statement_transaction(self.conn,'historic','2026-09-10',-22,'Different merchant')

    def test_distinct_same_batch_ids_keep_real_repeated_purchases(self):
        self.save([self.txn('one','2026-09-15',-22,'New coffee'),self.txn('two','2026-09-15',-22,'New coffee')])
        self.map()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM bank_transactions WHERE status='imported'").fetchone()[0],2)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0],3)

    def test_missing_pending_ids_keep_multiplicity_and_do_not_become_booked(self):
        raw={'id':None,'accountId':42,'date':'2026-09-15','amount':-3,'currency':'GBP','isPending':True}
        rows=client.transactions({'transactions':[raw,raw]},'42')
        self.assertNotEqual(rows[0]['provider_id'],rows[1]['provider_id'])
        self.save(rows)
        self.map()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0],1)
        with self.assertRaises(client.ProviderError):
            client.transactions({'transactions':[{**raw,'isPending':False}]},'42')

    def test_omitted_mapping_multipliers_preserve_existing_directions(self):
        self.save([self.txn(amount=12.34)])
        store.map_account(self.conn,'42',account_id='historic',amount_multiplier=-1,balance_multiplier=-1)
        store.map_account(self.conn,'42',account_id='historic')
        self.assertEqual(self.conn.execute('SELECT amount_multiplier,balance_multiplier FROM bank_connections').fetchone()[:],(-1,-1))

    def test_new_credit_requires_explicit_type_and_balance_direction(self):
        self.conn.execute("UPDATE bank_connections SET institution_name='New Bank',name='Credit card'")
        self.save([])
        self.assertIsNone(self.conn.execute('SELECT account_id FROM bank_connections').fetchone()[0])
        with self.assertRaises(ValueError):
            store.map_account(self.conn,'42',create_new=True)
        store.map_account(self.conn,'42',create_new=True,account_type='credit',balance_multiplier=-1)
        account=self.conn.execute('SELECT account_id FROM bank_connections').fetchone()[0]
        self.assertEqual(store.latest_balances(self.conn)[account]['amount'],-100)

    def test_exact_day_mutual_unique_matches_precede_shifted_repeats(self):
        self.conn.execute("INSERT INTO transactions(id,account_id,statement_id,txn_date,description,merchant,amount,dedupe_key) VALUES(2,'historic',1,'2026-08-30','COFFEE SHOP','Coffee Shop',-12.34,'second-key')")
        self.save([self.txn('day1','2026-08-29'),self.txn('day2','2026-08-30')])
        self.map()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM bank_transactions WHERE status='matched'").fetchone()[0],2)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0],2)

    def test_persisted_lock_cooldown_and_disabled_connection_stale(self):
        owner,run=store.acquire_sync(self.conn)
        with self.assertRaises(ValueError):
            store.acquire_sync(self.conn)
        store.finish_sync(self.conn,owner,run)
        with self.assertRaises(ValueError):
            store.acquire_sync(self.conn)
        self.save([self.txn()])
        self.map()
        self.conn.execute("UPDATE bank_connections SET status='EXPIRED'")
        self.assertTrue(store.latest_balances(self.conn)['historic']['stale'])
        self.assertGreater(seconds_until_due(store.sync_state(self.conn),21600),20000)


if __name__=='__main__':
    unittest.main()

class LunchFlowSyncTests(unittest.TestCase):
    def test_idempotent_sync_retries_derived_updates_after_failure(self):
        from unittest.mock import patch
        from app import lunchflow_routes as routes
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'bank.db'
            init_db(path).close()

            def reader(operation, account_id=None):
                if operation == 'accounts':
                    return {'accounts': [{'id': 42, 'name': 'Current', 'institution_name': 'Example Bank', 'status': 'ACTIVE'}]}
                if operation == 'balance':
                    return {'balance': {'amount': 125, 'currency': 'GBP'}}
                return {'transactions': [{'id': 'tx', 'accountId': 42, 'amount': -10, 'currency': 'GBP',
                    'date': '2026-09-18', 'description': 'Synthetic purchase', 'isPending': False}]}

            with patch.object(routes, 'refresh_derived', side_effect=RuntimeError('Synthetic failure')):
                failed = routes.run_sync(reader=reader, db_path=path)
            self.assertIn('derived spending insights', failed['result']['error'])
            conn = store.database(path)
            try:
                self.assertEqual(conn.execute('SELECT count(*) FROM transactions').fetchone()[0], 1)
                self.assertEqual(conn.execute('SELECT after_id FROM bank_derived_refresh_work').fetchone()[0], 0)
                conn.execute('UPDATE bank_sync_lock SET last_attempt_at=NULL')
                conn.commit()
                with patch.object(routes, 'refresh_derived', wraps=routes.refresh_derived) as refresh:
                    retried = routes.run_sync(reader=reader, db_path=path)
                self.assertIsNone(retried['result']['error'])
                self.assertEqual(refresh.call_args.args[1], 0)
                self.assertEqual(conn.execute('SELECT count(*) FROM transactions').fetchone()[0], 1)
                self.assertEqual(conn.execute('SELECT count(*) FROM bank_derived_refresh_work').fetchone()[0], 0)
            finally:
                conn.close()

    def test_sync_preserves_cache_on_provider_failure_and_fetches_amex(self):
        from app.lunchflow_routes import run_sync
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'bank.db'
            conn=init_db(path)
            conn.close()
            calls=[]
            def reader(operation,account_id=None):
                calls.append((operation,account_id))
                if operation=='accounts':
                    return {'accounts':[{'id':42,'name':'Current','institution_name':'Example Bank','status':'ACTIVE'},
                                        {'id':43,'name':'Gold Card','institution_name':'American Express','status':'ACTIVE'}]}
                if operation=='balance':
                    return {'balance':{'amount':125,'currency':'GBP'}}
                return {'transactions':[{'id':'tx','accountId':int(account_id),'amount':-10,'currency':'GBP','date':'2026-09-18','description':'Synthetic purchase','isPending':False}]}
            result=run_sync(reader=reader,db_path=path)
            self.assertEqual(result['stats']['imported'],1)
            self.assertIn(('balance','43'),calls)
            self.assertIn(('transactions','43'),calls)
            self.assertEqual(result['result']['accounts_read'],2)
            self.assertEqual(result['result']['accounts_failed'],0)
            amex=next(account for account in result['accounts'] if account['id']=='43')
            self.assertFalse(amex['excluded'])
            self.assertTrue(amex['requires_balance_direction'])
            self.assertIsNone(amex['account_id'])
            self.assertEqual(result['totals_by_currency']['GBP'],125)
            conn=store.database(path)
            baseline=[dict(row) for row in conn.execute('SELECT * FROM transactions')]
            conn.execute('UPDATE bank_sync_lock SET last_attempt_at=NULL')
            conn.commit()
            def failing(operation,account_id=None):
                raise RuntimeError('secret must never be returned')
            failed=run_sync(reader=failing,db_path=path)
            self.assertNotIn('secret',json.dumps(failed))
            self.assertEqual(failed['stats']['imported'],1)
            self.assertEqual([dict(row) for row in conn.execute('SELECT * FROM transactions')],baseline)
            self.assertFalse(failed['sync']['running'])
            conn.close()

class LunchFlowRetryTests(unittest.TestCase):
    def test_transient_failure_retries_after_fifteen_minutes(self):
        from datetime import datetime, timezone
        current = datetime(2026, 9, 21, 10, 10, tzinfo=timezone.utc)
        state = {'last_attempt_at': '2026-09-21T10:00:00Z', 'error': 'Lunch Flow is temporarily unavailable. Your saved data is unchanged.'}
        self.assertEqual(seconds_until_due(state, 21600, current), 300)
        state['last_attempt_at'] = '2026-09-21T09:00:00Z'
        self.assertEqual(seconds_until_due(state, 21600, current), 0)

    def test_success_and_credentials_keep_normal_schedule(self):
        from datetime import datetime, timezone
        current = datetime(2026, 9, 21, 10, 10, tzinfo=timezone.utc)
        for error in (None, 'Lunch Flow rejected the API key. Check the server configuration.'):
            self.assertEqual(seconds_until_due({'last_attempt_at': '2026-09-21T10:00:00Z', 'error': error}, 21600, current), 21000)

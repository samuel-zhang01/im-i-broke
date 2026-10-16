"""Amex activation keeps credit semantics explicit and original sources intact."""
import json
from pathlib import Path
import tempfile
import unittest

from app import lunchflow_client as client, lunchflow_store as store
from app.db import init_db


class AmexActivationTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/'test.db'
        self.conn=init_db(self.path);self.addCleanup(self.conn.close)
        store.ensure_schema(self.conn)
        self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('card','AMEX','credit','Saved card')")
        self.conn.execute("INSERT INTO statements(id,account_id,source_file,period_start,period_end,closing_balance,reconciled) VALUES(1,'card','amex.pdf','2026-08-01','2026-08-31',100,1)")
        self.conn.executemany("INSERT INTO transactions(id,account_id,statement_id,txn_date,description,merchant,amount,category,category_source,dedupe_key) VALUES(?,'card',1,?,?,?,?,?,'user',?)",[
            (1,'2026-08-15','BOOK SHOP','Book Shop',-12.34,'User books','original-purchase'),
            (2,'2026-08-20','PAYMENT RECEIVED','Payment Received',50,'User repayment','original-repayment')])
        self.original=[dict(row) for row in self.conn.execute('SELECT * FROM transactions ORDER BY id')]
        self.accounts=client.list_accounts({'accounts':[{'id':43,'name':'Gold Card','institution_name':'American Express','status':'ACTIVE'}]})
        store.discover_accounts(self.conn,self.accounts,'2026-09-19T08:00:00+00:00')
        self.conn.commit()

    def save(self,rows=None,balance=100,stamp='2026-09-19T08:10:00+00:00'):
        store.record_account_result(self.conn,'43',{'balance':{'amount':balance,'currency':'GBP'},
            'transactions':client.transactions({'transactions':rows or []},'43'),'errors':[]},stamp)

    def row(self,key,day,amount,description,pending=False):
        return {'id':key,'accountId':43,'date':day,'amount':amount,'currency':'GBP',
                'merchant':description,'description':description,'isPending':pending}

    def test_generic_amex_name_is_available_but_never_automatically_cash(self):
        self.conn.execute("DELETE FROM transactions")
        self.conn.execute("DELETE FROM statements")
        self.conn.execute("DELETE FROM accounts")
        self.save([self.row('purchase','2026-09-18',-25,'New shop')])
        account=dict(self.conn.execute('SELECT * FROM bank_connections').fetchone())
        self.assertEqual(account['excluded'],0)
        self.assertTrue(store.needs_balance_direction(account))
        self.assertIsNone(account['account_id'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM accounts').fetchone()[0],0)
        with self.assertRaises(ValueError):store.create_local_account(self.conn,account)
        with self.assertRaises(ValueError):store.map_account(self.conn,'43',create_new=True,account_type='cash',balance_multiplier=-1)
        with self.assertRaises(ValueError):store.map_account(self.conn,'43',create_new=True,account_type='credit')
        view=store.summary(self.conn)
        self.assertEqual(view['totals_by_currency'],{})
        self.assertEqual(view['balance_totals_by_currency'],{})
        self.assertEqual(view['institutions'][0]['totals_by_currency'],{})
        self.assertIsNone(view['accounts'][0]['balance']['amount'])
        self.assertEqual(view['accounts'][0]['balance']['reported_amount'],100)
        self.assertEqual(view['accounts'][0]['balance']['provider_reported_amount'],100)
        self.assertTrue(view['accounts'][0]['requires_balance_direction'])
        self.assertEqual(store.detail(self.conn,'43')['total'],1)

    def test_initial_credit_mapping_requires_direction_and_blocks_type_downgrade(self):
        self.save()
        with self.assertRaisesRegex(ValueError,'balance direction'):store.map_account(self.conn,'43',account_id='card')
        for kind in ('cash','current','savings'):
            with self.subTest(kind=kind),self.assertRaisesRegex(ValueError,'credit account type'):
                store.map_account(self.conn,'43',account_id='card',account_type=kind,balance_multiplier=-1)
        self.assertIsNone(self.conn.execute('SELECT account_id FROM bank_connections').fetchone()[0])
        self.assertEqual(self.conn.execute('SELECT account_type FROM accounts').fetchone()[0],'credit')
        store.map_account(self.conn,'43',account_id='card',balance_multiplier=-1)
        store.map_account(self.conn,'43',account_id='card')
        self.assertEqual(self.conn.execute('SELECT amount_multiplier,balance_multiplier FROM bank_connections').fetchone()[:],(1,-1))

    def test_mapping_cannot_reclassify_historical_cash_as_credit(self):
        self.conn.execute("UPDATE accounts SET account_type='current'")
        self.save()
        before = {table: [tuple(row) for row in self.conn.execute('SELECT * FROM ' + table)]
                  for table in ('accounts', 'statements', 'transactions', 'account_settings',
                                'account_settings_history', 'bank_connections')}
        with self.assertRaisesRegex(ValueError,'credit account type'):
            store.map_account(self.conn,'43',account_id='card',balance_multiplier=-1)
        with self.assertRaisesRegex(ValueError,'financial history'):
            store.map_account(self.conn,'43',account_id='card',account_type='credit',balance_multiplier=-1)
        after = {table: [tuple(row) for row in self.conn.execute('SELECT * FROM ' + table)] for table in before}
        self.assertEqual(before, after)

    def test_generic_existing_credit_target_also_requires_direction(self):
        self.conn.execute("UPDATE bank_connections SET institution_name='Example Bank'")
        self.conn.execute("UPDATE accounts SET bank='Example Bank'")
        self.save()
        with self.assertRaisesRegex(ValueError,'balance direction'):store.map_account(self.conn,'43',account_id='card')
        self.assertTrue(store.account_view(self.conn,self.conn.execute('SELECT * FROM bank_connections').fetchone())['requires_balance_direction'])
        self.assertEqual(store.summary(self.conn)['totals_by_currency'],{})

    def test_new_generic_card_explicit_credit_type_also_requires_direction(self):
        self.conn.execute("UPDATE bank_connections SET institution_name='New Bank',name='Everyday card'")
        self.conn.execute("UPDATE bank_connections SET currency='GBP'")
        with self.assertRaisesRegex(ValueError,'balance direction'):
            store.map_account(self.conn,'43',create_new=True,account_type='credit')
        self.assertIsNone(self.conn.execute('SELECT account_id FROM bank_connections').fetchone()[0])

    def test_backfill_preserves_statement_rows_and_signed_purchase_and_repayment(self):
        for multiplier in (1,-1):
            with self.subTest(multiplier=multiplier):
                self.conn.execute('SAVEPOINT scenario')
                rows=[self.row('old-purchase','2026-08-16',-12.34*multiplier,'Book Shop'),
                      self.row('old-repayment','2026-08-20',50*multiplier,'Payment Received'),
                      self.row('new-purchase','2026-09-15',-25*multiplier,'New shop'),
                      self.row('new-repayment','2026-09-17',60*multiplier,'Payment Received'),
                      self.row('pending','2026-09-18',-7*multiplier,'Pending hold',True)]
                self.save(rows)
                store.map_account(self.conn,'43',account_id='card',amount_multiplier=multiplier,balance_multiplier=-1)
                self.save(rows,stamp='2026-09-19T09:00:00+00:00')
                store.map_account(self.conn,'43',account_id='card')
                self.assertEqual([dict(row) for row in self.conn.execute('SELECT * FROM transactions WHERE id<=2 ORDER BY id')],self.original)
                self.assertEqual([row[0] for row in self.conn.execute('SELECT amount FROM transactions WHERE id>2 ORDER BY id')],[-25,60])
                self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM bank_transactions WHERE status='matched'").fetchone()[0],2)
                self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM bank_transactions WHERE status='pending' AND transaction_id IS NULL").fetchone()[0],1)
                self.assertEqual(json.loads(self.conn.execute("SELECT raw FROM bank_transactions WHERE provider_id='new-purchase'").fetchone()[0])['amount'],-25*multiplier)
                self.save(rows,balance=-20,stamp='2026-09-19T10:00:00+00:00')
                self.assertEqual(store.latest_balances(self.conn,resolve=False)['card']['amount'],20)
                self.assertEqual(self.conn.execute('SELECT amount FROM bank_balance_observations ORDER BY id DESC LIMIT 1').fetchone()[0],-20)
                self.conn.execute('ROLLBACK TO scenario');self.conn.execute('RELEASE scenario')

    def test_generic_exclusion_survives_discovery_and_prevents_sync(self):
        from app.lunchflow_routes import run_sync
        self.conn.execute('UPDATE bank_connections SET excluded=1')
        self.conn.commit()
        calls=[]
        def reader(operation,account_id=None):
            calls.append((operation,account_id))
            if operation=='accounts':return {'accounts':[row['raw'] for row in self.accounts]}
            raise AssertionError('Excluded connection must not be fetched')
        result=run_sync(reader=reader,db_path=self.path)
        self.assertEqual(calls,[('accounts',None)])
        self.assertTrue(result['accounts'][0]['excluded'])
        with self.assertRaisesRegex(ValueError,'This connection is excluded'):
            store.map_account(self.conn,'43',account_id='card',balance_multiplier=-1)


if __name__=='__main__':
    unittest.main()

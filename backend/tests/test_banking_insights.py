import json
from pathlib import Path
import tempfile
import unittest

from app.db import init_db
from app import banking_insights, lunchflow_store, merchants
from scripts.apply_merchant_research import apply_research


class BankingInsightTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.conn=init_db(Path(self.temp.name)/'test.db');self.addCleanup(self.conn.close)
        lunchflow_store.ensure_schema(self.conn)
        self.conn.executemany('INSERT INTO accounts(id,bank,account_type,display_name) VALUES(?,?,?,?)',[
            ('bank','Synthetic','current','Synthetic bank'),('broker','Synthetic','investment','Synthetic broker')])
        self.merchant=self.conn.execute("INSERT INTO merchants(canonical_name,kind,category,spending_treatment,source) VALUES('Fixture Bakery','merchant','Eating out','normal','user')").lastrowid
        self.conn.commit()

    def txn(self, amount, *, identity=None, account='bank', category='Uncategorized', key=None):
        cur=self.conn.execute('''INSERT INTO transactions(account_id,txn_date,description,merchant,source_merchant,amount,category,category_source,dedupe_key,classification,type_code)
            VALUES(?,'2026-09-01','Fixture Bakery','Fixture Bakery','Fixture Bakery',?,?,'auto',?,?,'CARD')''',
            (account,amount,category,key,json.dumps({'merchant_id':identity}) if identity else '{}'))
        return cur.lastrowid

    def cache(self, txn_id):
        self.conn.execute("""INSERT INTO bank_connections(id,account_id,name,institution_name,currency,status,raw,first_seen_at,last_seen_at)
            VALUES('feed','bank','Bank','Synthetic','GBP','ACTIVE','secret raw','2026-09-01','2026-09-01')""")
        self.conn.execute("""INSERT INTO bank_transactions(connection_id,provider_id,txn_date,amount,currency,merchant,description,status,transaction_id,raw,first_seen_at,last_seen_at)
            VALUES('feed','secret provider id','2026-09-02',-12,'GBP','123456789123456789','Fixture Bakery London','matched',?,'secret raw','2026-09-03','2026-09-03')""",(txn_id,))

    def test_profile_joins_saved_identity_only_and_excludes_investments(self):
        first=self.txn(-12,identity=self.merchant);self.txn(3,identity=self.merchant)
        self.txn(-200,identity=self.merchant,account='broker');self.txn(-999)
        self.cache(first)
        result=banking_insights.merchant_activity(self.conn,self.merchant)
        self.assertEqual(result['summary']['transactions'],2)
        self.assertEqual(result['summary']['money_out'],12)
        self.assertEqual(result['summary']['money_in'],3)
        self.assertEqual(result['summary']['net'],-9)
        self.assertEqual(result['bank_labels'][0]['label'],'Fixture Bakery')
        self.assertNotIn('secret',json.dumps(result))
        self.assertNotIn('raw',result['transactions'][0])

    def test_evidence_is_linked_not_inferred_and_ref_is_not_merchant(self):
        first=self.txn(-12,identity=self.merchant);other=self.txn(-12,identity=self.merchant)
        self.cache(first)
        result=banking_insights.transaction_evidence(self.conn,first)
        self.assertEqual(result['merchant'],'Fixture Bakery')
        self.assertEqual(result['merchant_source'],'description')
        self.assertEqual(result['date'],'2026-09-02')
        self.assertNotIn('secret',json.dumps(result))
        self.assertIsNone(banking_insights.transaction_evidence(self.conn,other))

    def test_grouped_directory_counts_stay_exact(self):
        self.txn(-12,identity=self.merchant);self.txn(3,identity=self.merchant);self.txn(-8)
        row=next(row for row in merchants.list_merchants(self.conn) if row['id']==self.merchant)
        self.assertEqual(row['n_transactions'],2)

    def test_researched_rules_touch_only_eligible_uncategorized_rows_and_are_idempotent(self):
        eligible=self.txn(-10,key='eligible');reviewed=self.txn(-20,key='reviewed')
        investor=self.txn(-30,account='broker',key='investor');classified=self.txn(-40,category='Shopping',key='classified')
        self.conn.execute("INSERT INTO overrides(dedupe_key,category) VALUES('reviewed','Uncategorized')")
        self.conn.commit()
        entries=[{'canonical_name':'Fixture Bakery','category':'Eating out','aliases':['Fixture Bakery'],
                  'source_url':'https://example.com/bakery','notes':'Synthetic test source.','action':'apply'}]
        before={row['id']:dict(row) for row in self.conn.execute('SELECT * FROM transactions')}
        result=apply_research(self.conn,entries)
        self.assertEqual(result['targeted_transactions'],1)
        self.assertEqual(self.conn.execute('SELECT category FROM transactions WHERE id=?',(eligible,)).fetchone()[0],'Eating out')
        # Derived analytics may update flags; source and decisions must remain.
        for ident in (reviewed,investor,classified):
            row=dict(self.conn.execute('SELECT * FROM transactions WHERE id=?',(ident,)).fetchone())
            for key in ('description','merchant','category','category_source','amount','meta','classification','dedupe_key'):
                self.assertEqual(row[key],before[ident][key])
        repeated=apply_research(self.conn,entries)
        self.assertEqual(repeated['targeted_transactions'],0)
        self.assertEqual(repeated['added_exact_aliases'],0)


if __name__=='__main__':unittest.main()

"""Bank references are not merchant names; raw bank evidence stays immutable."""
import json
from pathlib import Path
import tempfile
import unittest

from app.db import init_db
from app import lunchflow_client as client,lunchflow_store as store


class BankMerchantQualityTests(unittest.TestCase):
    def test_quality_distinguishes_references_placeholders_and_unicode_names(self):
        for label in ('123456789012345678901','1234-5678-9012','GB291234567890123456','123456789012REF'):
            self.assertEqual(client.merchant_label_quality(label),'reference')
        for label in ('Payment','CARD PAYMENT','Unknown','N/A','Not provided'):
            self.assertEqual(client.merchant_label_quality(label),'generic')
        for label in ('7-Eleven','H&M','M&S','無印良品','B2B Supplies'):
            self.assertEqual(client.merchant_label_quality(label),'usable')
        self.assertEqual(client.merchant_label_quality(None),'missing')

    def test_description_fallback_preserves_useful_provider_names_and_non_latin_text(self):
        self.assertEqual(client.merchant_label('123456789012345678901','SAINSBURYS LONDON')['merchant'],'Sainsburys')
        self.assertEqual(client.merchant_label('Verified Name','Unrelated text')['merchant'],'Verified Name')
        self.assertEqual(client.merchant_label('000000','無印良品')['merchant'],'無印良品')
        self.assertEqual(client.merchant_label(None,'123456')['merchant'],'Bank transaction')

    def test_cached_display_and_future_import_preserve_raw_reference_and_statement(self):
        with tempfile.TemporaryDirectory() as folder:
            conn=init_db(Path(folder)/'test.db')
            self.addCleanup(conn.close)
            store.ensure_schema(conn)
            accounts=client.list_accounts({'accounts':[{'id':42,'name':'Current','institution_name':'Example Bank','status':'ACTIVE','currency':'GBP'}]})
            store.discover_accounts(conn,accounts,'2026-09-18T10:00:00+00:00')
            raw={'id':'tx','accountId':42,'date':'2026-09-18','amount':-15,'currency':'GBP','merchant':'123456789012345678901','description':'SAINSBURYS LONDON','isPending':False}
            rows=client.transactions({'transactions':[raw]},'42')
            self.assertEqual(rows[0]['merchant'],raw['merchant'])
            self.assertEqual(rows[0]['raw'],raw)
            store.record_account_result(conn,'42',{'balance':{'amount':100,'currency':'GBP'},'transactions':rows,'errors':[]},'2026-09-18T10:01:00+00:00')
            cached=dict(conn.execute('SELECT * FROM bank_transactions').fetchone())
            before=dict(cached)
            view=store.transaction_view(cached)
            self.assertEqual(view['merchant'],'Sainsburys')
            self.assertEqual(view['merchant_source'],'description')
            self.assertEqual(view['merchant_quality'],'reference')
            self.assertEqual(cached,before)
            ledger=dict(conn.execute('SELECT * FROM transactions').fetchone())
            self.assertEqual(ledger['source_merchant'],'Sainsburys')
            self.assertEqual(ledger['category'],'Groceries')
            self.assertEqual(json.loads(ledger['raw']),raw)
            self.assertEqual(json.loads(ledger['meta'])['provider_merchant'],raw['merchant'])
            self.assertEqual(conn.execute('SELECT merchant FROM bank_transactions').fetchone()[0],raw['merchant'])
            self.assertEqual(store.merchant_label_repairs(conn),[])

            # Simulate a pre-upgrade feed row. Only a proposal is returned; the
            # caller owns migration and reviewed fields are not included.
            conn.execute('UPDATE transactions SET source_merchant=?,merchant=?,meta=?',
                         (raw['merchant'],raw['merchant'],json.dumps({'source':'lunchflow','source_merchant':raw['merchant'],'preserved':True})))
            old=dict(conn.execute('SELECT * FROM transactions').fetchone())
            proposals=store.merchant_label_repairs(conn)
            self.assertEqual(len(proposals),1)
            self.assertEqual(set(proposals[0]),{'transaction_id','source_merchant','meta'})
            self.assertTrue(json.loads(proposals[0]['meta'])['preserved'])
            self.assertEqual(dict(conn.execute('SELECT * FROM transactions').fetchone()),old)
            conn.execute("UPDATE transactions SET category_source='user',merchant='My correction'")
            self.assertEqual(store.merchant_label_repairs(conn),[])
            conn.execute("UPDATE transactions SET category_source='auto'")
            conn.execute("INSERT INTO overrides(dedupe_key,merchant) VALUES(?,?)",(old['dedupe_key'],'Saved correction'))
            self.assertEqual(store.merchant_label_repairs(conn),[])


if __name__=='__main__':
    unittest.main()

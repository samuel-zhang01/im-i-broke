"""CSV account choices must preserve both the ledger grain and future routing."""
import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.db import init_db
from app.ingest import ingest_dir, ingest_file
from app.portfolio import holding_positions
from app.trading212_accounts import ACCOUNTS


class Trading212AccountTests(unittest.TestCase):
    def setUp(self):
        # Functional API fixtures use local mode; auth/security have dedicated suites.
        self.enterContext(patch.dict('os.environ', {'FINANCE_AUTH_ENABLED': 'false', 'FINANCE_DEPLOYMENT': 'local'}))
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root/'ledger.db'
        self.c = init_db(self.path)

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def csv(self, filename, event_id='EOF1', units=2):
        path = self.root/filename
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {'Action':'Market buy','Time (UTC)':'2025-07-21 10:20:14+00:00',
               'ISIN':'IE0000000001','Ticker':'TEST','ID':event_id,'No. of shares':str(units),
               'Price / share':'10','Currency (Price / share)':'GBP','Exchange rate':'1',
               'Total':str(units*10),'Currency (Total)':'GBP'}
        with path.open('w',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=list(row));writer.writeheader();writer.writerow(row)
        return path

    def import_(self,path,kind=None):
        result=ingest_file(str(path),conn=self.c,trading212_account=kind)
        self.c.commit()
        return result

    def both(self):
        stocks=self.csv('stocks.csv')
        isa=self.csv('isa.csv','EOF2',3)
        self.import_(stocks,'stocks');self.import_(isa,'isa')
        return stocks,isa

    def test_same_security_and_provider_id_stay_separate_between_accounts(self):
        self.import_(self.csv('stocks.csv'),'stocks')
        self.import_(self.csv('isa.csv',units=3),'isa')
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM transactions').fetchone()[0],2)
        for kind, shares, cost in [('stocks',2,20),('isa',3,30)]:
            p=holding_positions(self.c,[ACCOUNTS[kind][0]])['IE0000000001']
            self.assertEqual((p['net_shares'],p['cost_basis']),(shares,cost))
        p=holding_positions(self.c)['IE0000000001']
        self.assertEqual((p['net_shares'],p['cost_basis']),(5,50))

    def test_new_unassigned_file_requires_choice_with_two_accounts_without_writes(self):
        self.both()
        before=self.c.total_changes
        with self.assertRaisesRegex(ValueError,'Choose Stocks'):
            self.import_(self.csv('unknown.csv','EOF3'))
        self.assertEqual(self.c.total_changes,before)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM statements').fetchone()[0],2)

    def test_known_renamed_export_reuses_account_and_deduplicates(self):
        _,isa=self.both()
        copied=self.root/'renamed.csv';shutil.copyfile(isa,copied)
        original=[tuple(r) for r in self.c.execute('SELECT id,account_id,dedupe_key,amount,raw FROM transactions ORDER BY id')]
        result=self.import_(copied)
        self.assertEqual((result['account_id'],result['new_txns']),('Trading212-isa',0))
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM statements').fetchone()[0],2)
        self.assertEqual(original,[tuple(r) for r in self.c.execute('SELECT id,account_id,dedupe_key,amount,raw FROM transactions ORDER BY id')])

    def test_known_source_cannot_be_silently_reassigned(self):
        _,isa=self.both()
        before=self.c.total_changes
        with self.assertRaisesRegex(ValueError,'already assigned'):
            self.import_(isa,'stocks')
        self.assertEqual(self.c.total_changes,before)

    def test_account_folders_retain_identity_on_fresh_and_repeat_bootstrap(self):
        self.csv('Data/Trading212/Stocks/first.csv')
        self.csv('Data/Trading212/ISA/second.csv','EOF2',3)
        result=ingest_dir(str(self.root/'Data'),self.path)
        self.assertEqual((result['new_transactions'],result['accounts']),(2,2))
        self.assertEqual(ingest_dir(str(self.root/'Data'),self.path)['new_transactions'],0)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM transactions WHERE account_id='Trading212-isa'").fetchone()[0],1)

    def test_legacy_single_account_id_remains_stable(self):
        result=self.import_(self.csv('original.csv'))
        self.assertEqual(result['account_id'],'Trading212-invest')
        self.import_(self.csv('later.csv','EOF2'))
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM accounts').fetchone()[0],1)

    def test_unsorted_folder_export_cannot_bypass_multiple_account_guard(self):
        self.both()
        self.csv('Data/Trading212/unknown.csv','EOF3',4)
        with self.assertRaisesRegex(ValueError,'Choose Stocks'):
            ingest_dir(str(self.root/'Data'),self.path)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM transactions').fetchone()[0],2)

    def test_operator_import_selects_isa_and_saved_assignment_survives_repeat(self):
        self.both()
        new = self.csv('new.csv', 'EOF3', 4)
        with self.assertRaisesRegex(ValueError, 'Choose Stocks'):
            ingest_file(str(new), conn=self.c)
        accepted = ingest_file(str(new), conn=self.c, trading212_account='isa')
        self.c.commit()
        self.assertEqual(accepted['account_id'], 'Trading212-isa')
        repeat = ingest_file(str(new), conn=self.c)
        self.assertEqual(repeat['new_txns'], 0)
        self.assertEqual(repeat['account_id'], 'Trading212-isa')



if __name__ == '__main__':
    unittest.main()

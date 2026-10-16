"""Synthetic regressions for changing broker CSV schemas and overlapping fills."""
import csv
import json
from pathlib import Path
import tempfile
import unittest

from app.db import init_db
from app.ingest import dedupe_key, ingest_file
from app.parsers import trading212


class Trading212UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.conn = init_db(self.root/'test.db')

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def write(self,name,rows):
        path=self.root/name
        fields=list(dict.fromkeys(k for row in rows for k in row))
        with path.open('w',newline='') as fh:
            writer=csv.DictWriter(fh,fieldnames=fields);writer.writeheader();writer.writerows(rows)
        return path

    def trade(self,**changes):
        return {'Action':'Market buy','Time':'2026-06-01 11:47:14','ISIN':'IE0000000001',
            'Ticker':'TEST','Name':'Synthetic ETF','ID':'EOF10001','No. of shares':'5',
            'Price / share':'21995','Currency (Price / share)':'GBX','Exchange rate':'100',
            'Total':'1099.75','Currency (Total)':'GBP',**changes}

    def dividend(self,time='2026-06-01 12:00:00'):
        return {'Action':'Dividend (Dividend)','Time':time,'ISIN':'US0000000001',
            'Ticker':'DIV','Name':'Synthetic dividend','ID':'','No. of shares':'2',
            'Price / share':'0.625','Currency (Price / share)':'USD','Exchange rate':'0.8',
            'Total':'1.00','Currency (Total)':'GBP'}

    def rows(self):
        return [dict(r) for r in self.conn.execute('SELECT * FROM transactions ORDER BY id')]

    def test_explicit_utc_header_price_units_and_clock_preserve_authoritative_total(self):
        row=self.trade();del row['Time'];row['Time (UTC)']='2026-06-01 12:47:14+01:00'
        event=trading212.parse(str(self.write('utc.csv',[row]))).txns[0]
        self.assertEqual(event.amount,-1099.75)
        self.assertEqual(event.ext_id,'EOF10001')
        self.assertEqual(event.meta['time'],'2026-06-01 11:47:14')
        self.assertEqual(event.meta['source_time'],'2026-06-01 12:47:14+01:00')
        self.assertEqual(event.meta['time_timezone'],'UTC')
        self.assertEqual((event.meta['execution_price'],event.meta['execution_price_currency']),(21995,'GBX'))
        self.assertEqual((event.meta['execution_price_major'],event.meta['execution_currency_major']),(219.95,'GBP'))
        self.assertEqual(event.meta['provider_id_kind'],'fill')
        self.assertEqual(event.meta['source_row'],row)
        self.assertAlmostEqual(event.meta['trade_value_check']['residual'],0)
        self.assertNotIn('order_id',event.meta)

    def test_trade_exchange_rate_direction_and_fees_are_not_applied_twice(self):
        row=self.trade(**{'Price / share':'120','Currency (Price / share)':'USD',
            'No. of shares':'1','Exchange rate':'1.2','Total':'100.15',
            'Currency conversion fee':'0.15','Currency (Currency conversion fee)':'GBP'})
        parsed=trading212.parse(str(self.write('fx.csv',[row,self.dividend()])))
        trade,dividend=parsed.txns
        self.assertEqual((trade.amount,trade.meta['cost_basis_gbp']),(-100.15,100.15))
        self.assertEqual(trade.meta['fees'],[{'name':'Currency conversion fee','amount':0.15,'currency':'GBP'}])
        self.assertEqual(trade.meta['exchange_rate_direction'],'price_currency_per_total_currency')
        self.assertIsNone(dividend.meta['exchange_rate_direction'])
        self.assertIsNone(dividend.meta['trade_value_check'])

    def test_case_sensitive_gbpence_quote_and_fees_are_not_pounds(self):
        row=self.trade(**{'Currency (Price / share)':'GBp', 'Total':'1100.00',
            'Transaction fee':'25', 'Currency (Transaction fee)':'GBp'})
        event=trading212.parse(str(self.write('pence.csv',[row]))).txns[0]
        self.assertEqual(event.amount,-1100)
        self.assertEqual(event.meta['execution_price_currency'],'GBX')
        self.assertEqual(event.meta['execution_price_major'],219.95)
        self.assertEqual(event.meta['fees'][0]['currency'],'GBX')
        self.assertAlmostEqual(event.meta['trade_value_check']['residual'],0)

    def test_lending_interest_is_income_without_changing_native_cash(self):
        row={'Action':'Lending interest','Time (UTC)':'2026-06-01 12:00:00',
             'Total':'1.25','Currency (Total)':'USD'}
        event=trading212.parse(str(self.write('lending.csv',[row]))).txns[0]
        self.assertEqual(event.category,'Investment income')
        self.assertEqual(event.amount,0)
        self.assertEqual(event.meta['cash_movements'],{'USD':1.25})

    def test_overlap_keeps_first_source_raw_ids_and_all_export_observations(self):
        first=self.write('old.csv',[self.trade(),self.dividend()])
        ingest_file(str(first),conn=self.conn);self.conn.commit()
        original=self.rows()
        # Simulate a legacy statement-text record that must never be replaced.
        self.conn.execute("UPDATE transactions SET raw='Original statement evidence' WHERE id=?",(original[0]['id'],))
        original=self.rows(); self.conn.commit()
        changed=[]
        for row in (self.trade(),self.dividend()):
            row['Time (UTC)']=row.pop('Time')+'+00:00';changed.append(row)
        latest=self.write('latest.csv',changed)
        self.assertEqual(ingest_file(str(latest),conn=self.conn)['new_txns'],0);self.conn.commit()
        for old,new in zip(original,self.rows()):
            for field in ('id','statement_id','raw','dedupe_key','amount'):
                self.assertEqual(old[field],new[field])
            observations=json.loads(new['meta'])['source_observations']
            self.assertEqual([o['source_file'] for o in observations],[str(first),str(latest)])
            self.assertTrue(all(o['source_sha256'] and o['source_row'] for o in observations))
        snapshot=self.rows()
        self.assertEqual(ingest_file(str(latest),conn=self.conn)['new_txns'],0);self.conn.commit()
        self.assertEqual(snapshot,self.rows())

    def test_no_id_same_day_same_amount_events_use_clock_across_partial_exports(self):
        early=self.dividend('2026-06-01 12:00:00');late=self.dividend('2026-06-01 13:00:00')
        full=self.write('full.csv',[early,late]);ingest_file(str(full),conn=self.conn);self.conn.commit()
        before=self.rows()
        partial=self.write('partial.csv',[late])
        self.assertEqual(ingest_file(str(partial),conn=self.conn)['new_txns'],0);self.conn.commit()
        self.assertEqual(len(self.rows()),2)
        self.assertEqual([r['id'] for r in self.rows()],[r['id'] for r in before])
        self.assertEqual(json.loads(self.rows()[0]['meta'])['source_observations'],json.loads(before[0]['meta'])['source_observations'])
        self.assertEqual(len(json.loads(self.rows()[1]['meta'])['source_observations']),2)

    def test_no_id_same_second_different_amounts_match_the_correct_partial_event(self):
        first=self.dividend();second={**self.dividend(),'Total':'2.00'}
        ingest_file(str(self.write('full.csv',[first,second])),conn=self.conn);self.conn.commit()
        before=self.rows()
        self.assertEqual(ingest_file(str(self.write('partial.csv',[second])),conn=self.conn)['new_txns'],0)
        self.conn.commit()
        after=self.rows()
        self.assertEqual([r['id'] for r in before],[r['id'] for r in after])
        self.assertEqual(len(json.loads(after[0]['meta'])['source_observations']),1)
        self.assertEqual(len(json.loads(after[1]['meta'])['source_observations']),2)

    def test_legacy_no_id_key_and_override_survive_utc_export(self):
        row=self.dividend();path=self.write('legacy.csv',[row]);ingest_file(str(path),conn=self.conn)
        stored=self.rows()[0];meta=json.loads(stored['meta'])
        legacy_key=dedupe_key('Trading212-invest','2026-06-01',1,stored['description'],0)
        for field in ('provider_id','source_observations'):meta.pop(field,None)
        self.conn.execute('UPDATE transactions SET dedupe_key=?,meta=? WHERE id=?',(legacy_key,json.dumps(meta),stored['id']))
        self.conn.execute("INSERT INTO overrides(dedupe_key,note) VALUES(?,'Keep review')",(legacy_key,));self.conn.commit()
        row['Time (UTC)']=row.pop('Time')+'+00:00'
        self.assertEqual(ingest_file(str(self.write('new.csv',[row])),conn=self.conn)['new_txns'],0);self.conn.commit()
        self.assertEqual((self.rows()[0]['id'],self.rows()[0]['dedupe_key']),(stored['id'],legacy_key))
        self.assertEqual(self.conn.execute('SELECT note FROM overrides WHERE dedupe_key=?',(legacy_key,)).fetchone()[0],'Keep review')

    def test_distinct_provider_fills_at_same_time_and_amount_never_collapse(self):
        path=self.write('fills.csv',[self.trade(),self.trade(ID='EOF10002')])
        self.assertEqual(ingest_file(str(path),conn=self.conn)['new_txns'],2)
        self.assertEqual(ingest_file(str(path),conn=self.conn)['new_txns'],0)
        self.assertEqual(len(self.rows()),2)

    def test_conflicting_provider_financial_revision_rolls_back_entire_file(self):
        first=self.write('first.csv',[self.trade()]);ingest_file(str(first),conn=self.conn);self.conn.commit()
        before=self.rows();statements=self.conn.execute('SELECT COUNT(*) FROM statements').fetchone()[0]
        revised=self.trade(**{'Total':'2199.50','No. of shares':'10'})
        correction=self.write('correction.csv',[self.trade(ID='EOF_NEW'),revised])
        with self.assertRaisesRegex(ValueError,'changed security or share quantity'):
            ingest_file(str(correction),conn=self.conn)
        self.assertEqual(before,self.rows())
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM statements').fetchone()[0],statements)

    def test_invalid_price_fx_fee_or_inconsistent_total_is_rejected(self):
        for changes in ({'Price / share':'NaN'},{'Exchange rate':'0'},
                        {'Currency conversion fee':'1','Currency (Currency conversion fee)':''},
                        {'Total':'21995'},{'Result':'NaN'}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                trading212.parse(str(self.write('bad.csv',[self.trade(**changes)])))

    def test_ambiguous_columns_or_extra_unquoted_values_are_rejected(self):
        path=self.root/'bad-columns.csv'
        path.write_text('Action,Time,Total,Currency (Total),Total\nDeposit,2026-06-01,100,GBP,1000\n')
        with self.assertRaisesRegex(ValueError,'duplicate column'):
            trading212.parse(str(path))
        path.write_text('Action,Time,Total,Currency (Total)\nDeposit,2026-06-01,100,GBP,unexpected\n')
        with self.assertRaisesRegex(ValueError,'more values than the header'):
            trading212.parse(str(path))


if __name__=='__main__':unittest.main()

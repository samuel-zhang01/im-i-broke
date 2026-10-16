"""Targeted operator repair must rehearse and preserve a live ledger atomically."""
from contextlib import closing
from datetime import date
import json
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

from app.db import init_db
from app.ingest import _store_statement
from app.parsers.base import ParsedStatement, RawTxn
from app.categorize import apply_categorization
from scripts import reprocess_hsbc as tool


class HsbcReprocessTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source = self.folder/'synthetic.db'
        self.pdf = self.folder/'synthetic-july.pdf'
        self.pdf.write_bytes(b'unchanged synthetic statement')
        self.conn = init_db(self.source)
        self.addCleanup(self.conn.close)
        with self.conn:
            _store_statement(self.conn, self.statement(1), str(self.pdf))
            key = self.conn.execute('SELECT dedupe_key FROM transactions').fetchone()[0]
            self.conn.execute("INSERT INTO overrides(dedupe_key,category,note) VALUES(?,'Groceries','Retain my review')", (key,))
            apply_categorization(self.conn)
        self.original = dict(self.conn.execute('SELECT * FROM transactions').fetchone())
        self.parsed = self.statement(29)
        self.enterContext(patch.object(tool, 'detect_bank', return_value=('HSBC Debit', None)))
        self.enterContext(patch.object(tool, 'ingest_file', side_effect=self.ingest))

    def statement(self, day):
        return ParsedStatement('HSBC','debit','HSBC-debit-12345678','11-22-33','12345678',
            date(2026,7,1),date(2026,7,31),100,90,
            [RawTxn(date(2026,7,day),'Synthetic shop',-10,raw='Synthetic shop',type_code='BP')])

    def ingest(self, path, conn):
        return _store_statement(conn, self.parsed, path)

    def run_tool(self, **options):
        return tool.reprocess(self.source, [self.pdf], **options)

    def test_preview_reports_dates_without_changing_source_or_printing_financial_rows(self):
        before = list(self.conn.iterdump())
        result = self.run_tool()
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertFalse(result['source_modified'])
        self.assertEqual(result['changes'], [{'transaction_id': self.original['id'],
            'previous_date':'2026-07-01','corrected_date':'2026-07-29'}])
        self.assertNotIn('Synthetic shop', json.dumps(result))
        self.assertNotIn('Retain my review', json.dumps(result))

    def test_apply_preserves_ids_keys_raw_reviews_and_is_repeatable(self):
        result = self.run_tool(apply=True, offline_source=True)
        self.assertEqual(result['mode'], 'applied')
        current = dict(self.conn.execute('SELECT * FROM transactions').fetchone())
        for field in ('id','account_id','amount','raw','dedupe_key','category','category_source','merchant'):
            self.assertEqual(current[field], self.original[field])
        self.assertEqual(current['txn_date'], '2026-07-29')
        self.assertEqual(json.loads(current['meta'])['date_corrections'][0]['previous_date'], '2026-07-01')
        self.assertEqual(self.conn.execute('SELECT note FROM overrides').fetchone()[0], 'Retain my review')
        self.assertEqual(self.run_tool(apply=True, offline_source=True)['changed_transaction_dates'], 0)
        self.assertEqual(self.conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(self.conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_apply_requires_stopped_writer_attestation(self):
        before = list(self.conn.iterdump())
        with self.assertRaisesRegex(ValueError, 'Stop all database writers'):
            self.run_tool(apply=True)
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_new_purchases_are_rejected_on_the_copy_before_live_writes(self):
        before = list(self.conn.iterdump())
        self.parsed = self.statement(1)
        self.parsed.txns.append(RawTxn(date(2026,7,30),'Additional synthetic purchase',-10))
        self.parsed.closing_balance = 80
        with self.assertRaisesRegex(ValueError, 'would add purchases'):
            self.run_tool(apply=True, offline_source=True)
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_unreconciled_and_non_hsbc_sources_cannot_be_applied(self):
        before = list(self.conn.iterdump())
        self.parsed.closing_balance = 999
        with self.assertRaisesRegex(ValueError, 'does not reconcile'):
            self.run_tool(apply=True, offline_source=True)
        with patch.object(tool, 'detect_bank', return_value=('AMEX',None)):
            with self.assertRaisesRegex(ValueError, 'Only HSBC'):
                self.run_tool(apply=True, offline_source=True)
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_reconciled_but_changed_statement_balances_cannot_be_applied(self):
        before = list(self.conn.iterdump())
        self.parsed.opening_balance = 200
        self.parsed.closing_balance = 190
        self.assertTrue(self.parsed.reconciles())
        with self.assertRaisesRegex(ValueError, 'printed balance'):
            self.run_tool(apply=True, offline_source=True)
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_changed_live_database_aborts_without_overwriting_the_other_writer(self):
        original_inspect = tool.inspect
        def edited(path):
            result = original_inspect(path)
            with self.conn:
                self.conn.execute("INSERT INTO preferences(key,value) VALUES('synthetic-concurrent-edit','retain')")
            return result
        with patch.object(tool, 'inspect', side_effect=edited):
            with self.assertRaisesRegex(ValueError, 'live database changed'):
                self.run_tool(apply=True, offline_source=True)
        self.assertEqual(self.conn.execute('SELECT txn_date FROM transactions').fetchone()[0], '2026-07-01')
        self.assertEqual(self.conn.execute("SELECT value FROM preferences WHERE key='synthetic-concurrent-edit'").fetchone()[0], 'retain')

    def test_changed_pdf_after_preview_aborts_before_live_writes(self):
        original_inspect = tool.inspect
        before = list(self.conn.iterdump())
        def changed(path):
            result = original_inspect(path)
            self.pdf.write_bytes(b'different synthetic PDF')
            return result
        with patch.object(tool, 'inspect', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'PDF changed'):
                self.run_tool(apply=True, offline_source=True)
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_apply_exception_rolls_back_live_changes(self):
        original_repair = tool._repair
        before = list(self.conn.iterdump())
        calls = []
        def fail_late(conn, files):
            result = original_repair(conn, files)
            calls.append(1)
            if len(calls) == 2:
                raise ValueError('Synthetic late failure after updating live dates')
            return result
        with patch.object(tool, '_repair', side_effect=fail_late):
            with self.assertRaisesRegex(ValueError, 'late failure'):
                self.run_tool(apply=True, offline_source=True)
        self.assertEqual(list(self.conn.iterdump()), before)


class HsbcReprocessCliTests(unittest.TestCase):
    def test_real_pdf_preview_and_apply_use_the_existing_account_and_ledger(self):
        # A small, valid PDF exercises pdfplumber and the actual CLI, without
        # importing a person's document or requiring another PDF dependency.
        import sys
        from app.parsers import hsbc_credit
        lines = ['HSBC','Statement Date 15 August 2026','1111 2222 3333 1234',
                 'Previous Balance 0.00','Debits 10.00','Credits 0.00','New Balance 10.00',
                 '1 Aug26 31 Jul26 SYNTHETIC SHOP 10.00']
        stream = ('BT /F1 12 Tf 40 750 Td '+ '\n'.join(
            ('0 -20 Td ' if index else '')+'('+line+') Tj' for index,line in enumerate(lines))+' ET').encode()
        objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
            b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
            b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
            b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream']
        document = bytearray(b'%PDF-1.4\n'); offsets = [0]
        for index, obj in enumerate(objects, 1):
            offsets.append(len(document)); document.extend(f'{index} 0 obj\n'.encode()+obj+b'\nendobj\n')
        xref = len(document)
        document.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
        document.extend(''.join(f'{offset:010d} 00000 n \n' for offset in offsets[1:]).encode())
        document.extend(f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
        with tempfile.TemporaryDirectory() as folder:
            pdf, source = Path(folder)/'synthetic.pdf', Path(folder)/'synthetic.db'
            pdf.write_bytes(document)
            parsed = hsbc_credit.parse(str(pdf))
            self.assertTrue(parsed.reconciles())
            parsed.txns[0].txn_date = date(2026,7,1)
            with closing(init_db(source)) as conn, conn:
                _store_statement(conn, parsed, str(pdf))
                original = dict(conn.execute('SELECT * FROM transactions').fetchone())
                apply_categorization(conn)
            environment = {key:value for key,value in os.environ.items()
                if not key.startswith(('FINANCE_','LUNCH_FLOW_','SNAPTRADE_','ENABLE_BANKING_'))}
            command = [sys.executable,'-m','scripts.reprocess_hsbc',str(pdf),'--database',str(source)]
            preview = subprocess.run(command, env=environment, capture_output=True, text=True, check=True)
            self.assertFalse(json.loads(preview.stdout)['source_modified'])
            with closing(init_db(source)) as conn:
                self.assertEqual(conn.execute('SELECT txn_date FROM transactions').fetchone()[0], '2026-07-01')
            applied = subprocess.run(command+['--apply','--offline-source'], env=environment,
                capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(applied.stdout)['changed_transaction_dates'], 1)
            with closing(init_db(source)) as conn:
                current = dict(conn.execute('SELECT * FROM transactions').fetchone())
                self.assertEqual(current['txn_date'], '2026-07-31')
                self.assertEqual((current['id'],current['dedupe_key'],current['raw']),
                                 (original['id'],original['dedupe_key'],original['raw']))

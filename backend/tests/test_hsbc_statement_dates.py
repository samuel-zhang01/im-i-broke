"""Printed calendar dates and card identities must survive layout variations."""
from contextlib import closing
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.db import init_db
from app.ingest import _store_statement
from app.parsers import hsbc_debit, hsbc_credit, amex
from app.parsers.base import ParsedStatement, RawTxn
from app import lunchflow_client as client, lunchflow_store as store


class Page:
    def __init__(self, text, words=()): self.text, self.words = text, words
    def extract_text(self): return self.text
    def extract_words(self): return self.words


class Pdf:
    def __init__(self, *pages): self.pages = pages
    def __enter__(self): return self
    def __exit__(self, *_): pass


class HsbcStatementDateTests(unittest.TestCase):
    def credit(self, lines):
        text = ('Statement Date 15 August 2026\n1111 2222 3333 1234\n'
                'Previous Balance 0.00\nDebits 10.00\nCredits 0.00\nNew Balance 10.00\n'+lines)
        with patch.object(hsbc_credit.pdfplumber, 'open', return_value=Pdf(Page(text))):
            return hsbc_credit.parse('synthetic.pdf')

    def test_credit_four_digit_year_never_becomes_2020(self):
        for text in ('29 July 2026', '29Jul2026', '29 Jul26', '29 Jul 26'):
            self.assertEqual(hsbc_credit._pdate(text), date(2026, 7, 29))
        parsed = self.credit('31 Jul 2026 29 Jul 2026 SYNTHETIC SHOP 10.00')
        self.assertEqual(parsed.txns[0].txn_date, date(2026, 7, 29))
        self.assertEqual(parsed.txns[0].meta['posting_date'], '2026-07-31')
        self.assertTrue(parsed.reconciles())

    def test_credit_honors_transaction_first_header_at_month_boundary(self):
        parsed = self.credit('Transaction Date Posting Date Description Amount\n31 July 2026 1 August 2026 SYNTHETIC SHOP 10.00')
        self.assertEqual(parsed.txns[0].txn_date, date(2026, 7, 31))
        self.assertEqual(parsed.txns[0].meta['posting_date'], '2026-08-01')

    def test_credit_invalid_dates_and_unreadable_amount_fail_instead_of_skipping(self):
        for line in ('31 Feb26 29 Jan26 SHOP 10.00', '16 Aug26 15 Aug26 SHOP 10.00',
                     '31 Jul26 29 Jul26 SHOP unreadable'):
            with self.subTest(line=line), self.assertRaises(ValueError): self.credit(line)

    def test_credit_spaced_cr_remains_a_refund(self):
        self.assertEqual(self.credit('31 Jul26 29 Jul26 REFUND 10.00 CR').txns[0].amount, 10)

    def test_debit_period_keeps_explicit_years_and_compact_months(self):
        for text in ('17 December 2025 to 16 January 2026', '17December to 16January2026'):
            meta = hsbc_debit._header_meta(Pdf(Page(text)))
            self.assertEqual(meta['period_start'], date(2025, 12, 17))
            self.assertEqual(meta['period_end'], date(2026, 1, 16))

    def test_debit_missing_year_resolves_inside_period_and_rejects_ambiguity(self):
        meta = {'period_start': date(2026, 7, 15), 'period_end': date(2026, 8, 14)}
        self.assertEqual(hsbc_debit._table_date('31Jul', meta, None), date(2026, 7, 31))
        self.assertEqual(hsbc_debit._table_date('1', meta, date(2026, 7, 31)), date(2026, 8, 1))
        for text in ('32Jul26', '14Jul26', 'bad date', '29 Foo', '29 Jully'):
            with self.assertRaises(ValueError): hsbc_debit._table_date(text, meta, None)
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            hsbc_debit._table_date('20', {'period_start': date(2026, 7, 1), 'period_end': date(2026, 8, 31)}, None)

    def test_debit_compact_date_words_do_not_inherit_statement_start(self):
        def word(text, x, y, right=None): return {'text': text, 'x0': x, 'x1': right or x+20, 'top': y}
        words = [word('Paidout', 360, 10), word('Paidin', 450, 10), word('Balance', 550, 10),
                 word('29Jul2026', 15, 20, 90), word('BP', 110, 20), word('SHOP', 150, 20), word('10.00', 360, 20, 400)]
        header = '12-34-56 12345678\n15 July to 14 August 2026\nOpening Balance 100.00\nClosing Balance 90.00'
        with patch.object(hsbc_debit.pdfplumber, 'open', return_value=Pdf(Page(header, words))):
            parsed = hsbc_debit.parse('synthetic.pdf')
        self.assertEqual(parsed.txns[0].txn_date, date(2026, 7, 29))
        self.assertTrue(parsed.reconciles())

    def test_debit_overdraft_header_is_negative(self):
        meta = hsbc_debit._header_meta(Pdf(Page('Opening Balance 100.00D\nClosing Balance -110.00')))
        self.assertEqual(meta['opening'], -100); self.assertEqual(meta['closing'], -110)

    def debit_with_opening_date(self, opening_date, transaction_date):
        def word(text, x, y, right=None):
            return {'text': text, 'x0': x, 'x1': right or x+20, 'top': y}
        words = [word('Paidout', 360, 10), word('Paidin', 450, 10), word('Balance', 550, 10),
                 word(opening_date, 15, 20, 90), word('BALANCEBROUGHTFORWARD', 150, 20),
                 word('100.00', 550, 20, 600),
                 word('BP', 110, 30), word('SHOP', 150, 30), word('10.00', 360, 30, 400),
                 word('90.00', 550, 30, 600)]
        if transaction_date:
            words.append(word(transaction_date, 15, 30, 90))
        header = ('12-34-56 12345678\n17 July to 16 August 2026\n'
                  'Opening Balance 100.00\nClosing Balance 90.00')
        with patch.object(hsbc_debit.pdfplumber, 'open', return_value=Pdf(Page(header, words))):
            return hsbc_debit.parse('synthetic.pdf')

    def test_debit_previous_day_opening_balance_does_not_reject_valid_transactions(self):
        parsed = self.debit_with_opening_date('16 Jul 26', '29Jul2026')
        self.assertEqual(len(parsed.txns), 1)
        self.assertEqual(parsed.txns[0].txn_date, date(2026, 7, 29))
        self.assertEqual(parsed.txns[0].meta['printed_date'], '29Jul2026')
        self.assertEqual(parsed.txns[0].balance, 90)
        self.assertTrue(parsed.reconciles())

    def test_debit_undated_activity_cannot_inherit_previous_period_opening_date(self):
        with self.assertRaisesRegex(ValueError, 'no printed date'):
            self.debit_with_opening_date('16 Jul 26', None)

    def test_debit_opening_date_inside_period_can_anchor_undated_activity(self):
        parsed = self.debit_with_opening_date('17Jul26', None)
        self.assertEqual(parsed.txns[0].txn_date, date(2026, 7, 17))
        self.assertTrue(parsed.reconciles())

    def test_debit_outside_transaction_date_still_fails_after_opening_balance(self):
        with self.assertRaisesRegex(ValueError, 'outside the printed statement period'):
            self.debit_with_opening_date('16 Jul 26', '16Jul26')

    def amex(self, member, first_page=False):
        header = f'Membership Number {member}\nStatement Period From 1July to31July2026\n£0.00 - £0.00 + £10.00 = £10.00\n'
        row = 'Jul29 Jul30 SYNTHETIC SHOP 10.00'
        pages = [Page(header+row)] if first_page else [Page(header), Page(row)]
        with patch.object(amex.pdfplumber, 'open', return_value=Pdf(*pages)):
            return amex.parse('synthetic.pdf')

    def test_amex_first_page_activity_is_reconciled(self):
        parsed = self.amex('1111-111111-12345', first_page=True)
        self.assertEqual(len(parsed.txns), 1); self.assertTrue(parsed.reconciles())

    def test_amex_suffix_collision_preserves_both_cards_and_reimports(self):
        with tempfile.TemporaryDirectory() as folder, closing(init_db(Path(folder)/'synthetic.db')) as conn:
            for index, member in enumerate(('1111-111111-12345', '2222-222222-12345')):
                self.assertEqual(_store_statement(conn, self.amex(member), f'card-{index}.pdf')['new_txns'], 1)
            self.assertEqual(_store_statement(conn, self.amex('2222-222222-12345'), 'card-1.pdf')['new_txns'], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM accounts').fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 2)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def statement(self, txn_date=date(2026, 7, 31)):
        return ParsedStatement('HSBC', 'credit', 'HSBC-credit-1234', None, '1111222233331234',
            None, date(2026, 8, 15), 0, 10,
            [RawTxn(txn_date, 'Synthetic shop', -10, type_code='DR',
                    raw='1 Aug 2026 31 Jul 2026 Synthetic shop 10.00', meta={'posting_date': '2026-08-01'})])

    def feed(self, conn):
        store.discover_accounts(conn, client.list_accounts({'accounts': [
            {'id': 'feed', 'name': 'Credit card', 'institution_name': 'HSBC', 'currency': 'GBP', 'status': 'ACTIVE'}]}),
            '2026-08-15T12:00:00+00:00')
        store.map_account(conn, 'feed', account_id='HSBC-credit-1234', balance_multiplier=-1)
        raw = {'id': 'purchase', 'accountId': 'feed', 'date': '2026-08-01', 'amount': -10,
               'currency': 'GBP', 'description': 'Synthetic shop', 'isPending': False}
        store.record_account_result(conn, 'feed', {'balance': {'amount': 10, 'currency': 'GBP'},
            'transactions': client.transactions({'transactions': [raw]}, 'feed'), 'errors': []},
            '2026-08-15T12:01:00+00:00')

    def test_july_purchase_date_is_independent_of_pdf_api_arrival_order(self):
        for pdf_first, purchase_day in ((True,31),(False,31),(True,20),(False,20)):
            with self.subTest(pdf_first=pdf_first,purchase_day=purchase_day), tempfile.TemporaryDirectory() as folder, closing(init_db(Path(folder)/'synthetic.db')) as conn:
                conn.execute("INSERT INTO accounts(id,bank,account_type,account_number) VALUES('HSBC-credit-1234','HSBC','credit','1111222233331234')")
                statement = self.statement(date(2026,7,purchase_day))
                if pdf_first: _store_statement(conn, statement, 'synthetic.pdf')
                self.feed(conn)
                before = dict(conn.execute('SELECT * FROM transactions').fetchone())
                with conn: conn.execute("UPDATE transactions SET category='My review',category_source='user'")
                _store_statement(conn, statement, 'synthetic.pdf')
                _store_statement(conn, statement, 'synthetic.pdf')
                self.feed(conn)
                after = dict(conn.execute('SELECT * FROM transactions').fetchone())
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 1)
                self.assertEqual(after['txn_date'], f'2026-07-{purchase_day}')
                self.assertEqual(after['id'], before['id']); self.assertEqual(after['dedupe_key'], before['dedupe_key'])
                self.assertEqual(after['raw'], before['raw']); self.assertEqual(after['category'], 'My review')
                cached = conn.execute('SELECT txn_date,raw FROM bank_transactions').fetchone()
                self.assertEqual(cached['txn_date'], '2026-08-01')
                self.assertEqual(json.loads(cached['raw'])['date'], '2026-08-01')
                if not pdf_first:
                    self.assertEqual(json.loads(after['meta'])['provider_transaction_date'], '2026-08-01')
                self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_unreconciled_pdf_does_not_rewrite_a_feed_date(self):
        with tempfile.TemporaryDirectory() as folder, closing(init_db(Path(folder)/'synthetic.db')) as conn:
            conn.execute("INSERT INTO accounts(id,bank,account_type,account_number) VALUES('HSBC-credit-1234','HSBC','credit','1111222233331234')")
            self.feed(conn)
            statement = self.statement(); statement.closing_balance = 999
            _store_statement(conn, statement, 'unverified.pdf')
            self.assertEqual(conn.execute('SELECT txn_date FROM transactions').fetchone()[0], '2026-08-01')

    def test_same_pdf_date_repair_retains_keys_reviews_and_correction_evidence(self):
        with tempfile.TemporaryDirectory() as folder, closing(init_db(Path(folder)/'synthetic.db')) as conn:
            source = Path(folder)/'synthetic.pdf'; source.write_bytes(b'synthetic unchanged source')
            _store_statement(conn, self.statement(date(2020, 7, 31)), str(source))
            original = dict(conn.execute('SELECT * FROM transactions').fetchone())
            with conn:
                conn.execute("INSERT INTO overrides(dedupe_key,category,note) VALUES(?,'My review','Keep this note')", (original['dedupe_key'],))
            _store_statement(conn, self.statement(), str(source))
            _store_statement(conn, self.statement(), str(source))
            after = dict(conn.execute('SELECT * FROM transactions').fetchone())
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 1)
            self.assertEqual((after['id'],after['dedupe_key']), (original['id'],original['dedupe_key']))
            self.assertEqual(after['txn_date'], '2026-07-31')
            self.assertEqual(conn.execute('SELECT note FROM overrides').fetchone()[0], 'Keep this note')
            corrections = json.loads(after['meta'])['date_corrections']
            self.assertEqual(len(corrections), 1)
            self.assertEqual(corrections[0]['previous_date'], '2020-07-31')
            self.assertEqual(corrections[0]['previous_raw'], original['raw'])
            # Content-addressed uploads reuse the same source and repair receipt.
            renamed = Path(folder)/'renamed.pdf'; renamed.write_bytes(source.read_bytes())
            _store_statement(conn, self.statement(), str(renamed))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM statements').fetchone()[0], 1)
            overlap = Path(folder)/'overlap.pdf'; overlap.write_bytes(b'other synthetic statement source')
            _store_statement(conn, self.statement(), str(overlap))
            after_overlap = dict(conn.execute('SELECT * FROM transactions').fetchone())
            self.assertEqual(after_overlap['id'], original['id'])
            self.assertEqual(json.loads(after_overlap['meta'])['date_corrections'], corrections)
            self.assertEqual(conn.execute('SELECT note FROM overrides').fetchone()[0], 'Keep this note')

    def test_conflicting_reparse_cannot_silently_duplicate_wrongly_dated_rows(self):
        with tempfile.TemporaryDirectory() as folder, closing(init_db(Path(folder)/'synthetic.db')) as conn:
            source = Path(folder)/'synthetic.pdf'; source.write_bytes(b'original synthetic source')
            _store_statement(conn, self.statement(date(2020, 7, 31)), str(source))
            conn.commit(); before = list(conn.iterdump())
            source.write_bytes(b'different synthetic source')
            with self.assertRaisesRegex(ValueError, 'cannot be matched safely'), conn:
                _store_statement(conn, self.statement(), str(source))
            self.assertEqual(list(conn.iterdump()), before)

    def test_hsbc_full_card_and_sort_code_collisions_keep_accounts_separate(self):
        with tempfile.TemporaryDirectory() as folder, closing(init_db(Path(folder)/'synthetic.db')) as conn:
            _store_statement(conn, self.statement(), 'first.pdf')
            other = self.statement(); other.account_number = '9999888877771234'
            _store_statement(conn, other, 'second.pdf')
            _store_statement(conn, self.statement(), 'first.pdf')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM accounts').fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 2)
            for index, sort_code in enumerate(('11-22-33','44-55-66')):
                debit = ParsedStatement('HSBC','debit','HSBC-debit-12345678',sort_code,'12345678',
                    date(2026,7,1),date(2026,7,31),10,0,[RawTxn(date(2026,7,29),'Synthetic shop',-10)])
                _store_statement(conn, debit, f'debit-{index}.pdf')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM accounts').fetchone()[0], 4)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 4)

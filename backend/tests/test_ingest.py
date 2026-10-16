import csv
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from app.db import init_db
from app.ingest import _store_statement, ingest_file, reconciled
from app.parsers import amex, hsbc_credit, hsbc_debit, trading212
from app.parsers.base import ParsedStatement, RawTxn


class AmexTests(unittest.TestCase):
    def test_statement_credit_on_separate_line_and_fx_fee_not_double_counted(self):
        text = ('Dec30 Dec31 SHOP PARIS 103.00\n'
                'Exchange Rate 1.15 +Nonsterling Transaction Fee 3.00\n'
                'Jan2 Jan2 PAYMENT RECEIVED - THANK YOU 50.00\nCR\n'
                'Jan2 Jan2 SHOP REFUND 3.00 CR\nTotal new spend 100.00')
        txns = amex._transactions(text, date(2026, 1, 3))
        self.assertEqual([t.amount for t in txns], [-103, 50, 3])
        self.assertEqual(txns[0].txn_date, date(2025, 12, 30))
        self.assertEqual(txns[0].meta['fx_fee_included_gbp'], 3)
        self.assertEqual(txns[1].category, 'Transfers & payments')
        self.assertIsNone(txns[2].category)

    def test_summary_parses_compact_months_and_year_boundary(self):
        meta = amex._header_meta('Membership Number xxxx-xxxxxx-12345\n'
                                '£100.00 - £50.00 + £25.00 = £75.00\n'
                                'Statement Period From 4December to3January2026')
        self.assertEqual(meta['start'], date(2025, 12, 4))
        self.assertEqual(meta['end'], date(2026, 1, 3))
        self.assertEqual(meta['closing'], 75)

    def test_malformed_transaction_is_rejected(self):
        with self.assertRaises(ValueError):
            amex._transactions('Jan1 Jan1 SHOP amount-unreadable', date(2026, 1, 3))


class ReconciliationTests(unittest.TestCase):
    def statement(self, opening=None, closing=None, txns=None):
        return ParsedStatement('Test', 'credit', 'Test-1234', None, '1234',
                               date(2026, 1, 1), date(2026, 1, 31), opening, closing, txns or [])

    def test_missing_balances_are_unknown_even_when_empty(self):
        self.assertIsNone(reconciled(self.statement()))
        self.assertFalse(reconciled(self.statement(5, 6)))
        self.assertTrue(reconciled(self.statement(5, 5)))

    def test_credit_balance_direction_and_independent_totals(self):
        s = self.statement(100, 125, [RawTxn(date(2026, 1, 2), 'Shop', -50),
                                    RawTxn(date(2026, 1, 3), 'Payment', 25)])
        self.assertTrue(reconciled(s))
        s.total_money_in = 35
        s.total_money_out = 60  # same net but a missing debit and credit
        self.assertFalse(reconciled(s))

    def test_hsbc_january_statement_starts_in_previous_year(self):
        fake_page = type('Page', (), {'extract_text': lambda self: '17 December to 16 January 2026'})()
        meta = hsbc_debit._header_meta(type('Pdf', (), {'pages': [fake_page]})())
        self.assertEqual(meta['period_start'], date(2025, 12, 17))


class HsbcCreditCoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = init_db(Path(self.tmp.name) / 'db.sqlite')

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def statement(self, end=date(2026, 1, 15), opening=0, closing=20, posting=date(2026, 1, 3)):
        amount = opening - closing
        txns = [RawTxn(posting, 'Shop', amount)] if amount else []
        s = ParsedStatement('HSBC', 'credit', 'HSBC-credit-1234', None, '1111222233331234',
                            None, end, opening, closing, txns,
                            total_money_in=max(amount, 0), total_money_out=max(-amount, 0))
        s.meta = {'parser': 'hsbc_credit', 'printed_card_number': s.account_number,
                  'printed_statement_date': end.isoformat(),
                  'printed_debits': s.total_money_out, 'printed_credits': s.total_money_in,
                  'posting_dates_valid': True,
                  'first_posting_date': posting.isoformat() if txns else None,
                  'last_posting_date': posting.isoformat() if txns else None}
        return s

    def current_start(self, source='feb.pdf'):
        return self.conn.execute('SELECT period_start FROM statements WHERE source_file=?', (source,)).fetchone()[0]

    def pair(self):
        first = self.statement()
        second = self.statement(date(2026, 2, 15), 20, 45, date(2026, 1, 19))
        _store_statement(self.conn, first, 'jan.pdf')
        _store_statement(self.conn, second, 'feb.pdf')
        return first, second

    def test_parser_preserves_printed_date_credit_balances_and_posting_evidence(self):
        text = ('Statement Date 15 February 2026\n1111 2222 3333 1234\n'
                'Previous Balance 20.00CR\nDebits 5.00\nCredits 0.00\nNew Balance 15.00CR\n'
                '19 Jan26 18 Jan26 SHOP 5.00')
        with patch.object(hsbc_credit.pdfplumber, 'open') as opened:
            opened.return_value.__enter__.return_value.pages = [type('Page', (), {'extract_text': lambda _: text})()]
            s = hsbc_credit.parse('2026-02-14_filename-is-not-the-date.pdf')
        self.assertTrue(s.reconciles())
        self.assertEqual(s.period_end, date(2026, 2, 15))
        self.assertIsNone(s.period_start)
        self.assertEqual(s.opening_balance, -20)
        self.assertEqual(s.txns[0].txn_date, date(2026, 1, 18))
        self.assertEqual(s.txns[0].meta['posting_date'], '2026-01-19')
        self.assertEqual(s.meta['first_posting_date'], '2026-01-19')

    def test_verified_sequence_covers_quiet_days_and_retains_provenance(self):
        self.pair()
        self.assertIsNone(self.current_start('jan.pdf'))
        self.assertEqual(self.current_start(), '2026-01-16')
        row = self.conn.execute("SELECT meta FROM statements WHERE source_file='feb.pdf'").fetchone()
        evidence = json.loads(row['meta'])['period_start_evidence']
        self.assertEqual(evidence['previous_period_end'], '2026-01-15')
        self.assertEqual(evidence['previous_closing_balance'], 20)

    def test_reverse_import_order_and_repeat_are_idempotent(self):
        first = self.statement()
        second = self.statement(date(2026, 2, 15), 20, 45, date(2026, 1, 19))
        _store_statement(self.conn, second, 'feb.pdf')
        self.assertIsNone(self.current_start())
        _store_statement(self.conn, first, 'jan.pdf')
        self.assertEqual(self.current_start(), '2026-01-16')
        ids = [r[0] for r in self.conn.execute('SELECT id FROM transactions ORDER BY id')]
        result = _store_statement(self.conn, second, 'feb.pdf')
        self.assertEqual(result['new_txns'], 0)
        self.assertEqual(result['period'][0], '2026-01-16')
        self.assertEqual(ids, [r[0] for r in self.conn.execute('SELECT id FROM transactions ORDER BY id')])

    def test_identical_pdf_upload_reuses_source_metadata_and_preserves_coverage(self):
        first, second = self.pair()
        source = Path(self.tmp.name) / 'original.pdf'
        uploaded = Path(self.tmp.name) / 'content-hash.pdf'
        source.write_bytes(b'Exact same credit statement PDF bytes')
        uploaded.write_bytes(source.read_bytes())
        # Move the fixture's first source to a real file before hashing it.
        self.conn.execute("UPDATE statements SET source_file=? WHERE source_file='jan.pdf'", (str(source),))
        _store_statement(self.conn, first, str(source))
        self.assertEqual(self.current_start(), '2026-01-16')
        result = _store_statement(self.conn, first, str(uploaded))
        self.assertEqual(result['new_txns'], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM statements').fetchone()[0], 2)
        self.assertEqual(self.current_start(), '2026-01-16')

    def test_mismatched_balance_revokes_derived_interval_on_reimport(self):
        first, second = self.pair()
        self.assertEqual(self.current_start(), '2026-01-16')
        # A corrected predecessor still reconciles independently, but no longer
        # carries the same balance into the next source.
        first.opening_balance = 1
        first.closing_balance = 21
        _store_statement(self.conn, first, 'jan.pdf')
        self.assertIsNone(self.current_start())
        meta = json.loads(self.conn.execute("SELECT meta FROM statements WHERE source_file='feb.pdf'").fetchone()[0])
        self.assertNotIn('period_start_evidence', meta)

    def test_missing_month_is_not_bridged_even_with_same_balance(self):
        _store_statement(self.conn, self.statement(), 'jan.pdf')
        march = self.statement(date(2026, 3, 15), 20, 45, date(2026, 3, 1))
        _store_statement(self.conn, march, 'march.pdf')
        self.assertIsNone(self.current_start('march.pdf'))

    def test_failed_unverified_or_missing_summary_evidence_cannot_establish_coverage(self):
        first, second = self.pair()
        for bad in ('failed', 'unverified', 'missing_totals', 'missing_dates', 'different_card'):
            with self.subTest(bad=bad):
                first = self.statement()
                if bad == 'failed':
                    first.closing_balance += 1
                elif bad == 'unverified':
                    first.opening_balance = None
                elif bad == 'missing_totals':
                    first.meta['printed_debits'] = None
                elif bad == 'missing_dates':
                    first.meta['posting_dates_valid'] = False
                else:
                    first.meta['printed_card_number'] = '9999888877771234'
                _store_statement(self.conn, first, 'jan.pdf')
                self.assertIsNone(self.current_start())

    def test_overlapping_postings_or_duplicate_statement_date_revoke_coverage(self):
        first, second = self.pair()
        second.meta['first_posting_date'] = '2026-01-15'
        _store_statement(self.conn, second, 'feb.pdf')
        self.assertIsNone(self.current_start())
        second.meta['first_posting_date'] = '2026-01-19'
        _store_statement(self.conn, second, 'feb.pdf')
        self.assertEqual(self.current_start(), '2026-01-16')
        _store_statement(self.conn, first, 'conflicting-jan.pdf')
        self.assertIsNone(self.current_start())

    def test_empty_verified_statement_can_cover_quiet_month_after_credit_balance(self):
        _store_statement(self.conn, self.statement(closing=-20), 'jan.pdf')
        second = self.statement(date(2026, 2, 15), -20, -20)
        _store_statement(self.conn, second, 'feb.pdf')
        self.assertEqual(self.current_start(), '2026-01-16')


class Trading212Tests(unittest.TestCase):
    def parse_rows(self, rows):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'events.csv'
            fields = sorted(set().union(*(row.keys() for row in rows)))
            with path.open('w', newline='') as fh:
                writer = csv.DictWriter(fh, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            return trading212.parse(str(path))

    def test_foreign_trade_retains_shares_without_inventing_gbp_cost(self):
        s = self.parse_rows([{'Action': 'Market buy', 'Time': '2026-01-03 12:34:56',
                             'Total': '100', 'Currency (Total)': 'USD', 'No. of shares': '2',
                             'ISIN': 'US0000000001', 'Ticker': 'TEST', 'Exchange rate': '1'}])
        t = s.txns[0]
        self.assertEqual(t.amount, 0)
        self.assertEqual(t.meta['cash_movements'], {'USD': -100})
        self.assertEqual(t.meta['shares'], 2)
        self.assertEqual(t.meta['time'], '2026-01-03 12:34:56')
        self.assertIsNone(t.meta['cost_basis_gbp'])
        self.assertIsNone(reconciled(s))

    def test_currency_conversion_cash_legs_and_fee_once(self):
        s = self.parse_rows([{'Action': 'Currency conversion', 'Time': '2026-01-03 12:00:00',
                             'Total': '-1.50', 'Currency (Total)': 'USD',
                             'Currency conversion from amount': '800',
                             'Currency (Currency conversion from amount)': 'GBP',
                             'Currency conversion to amount': '1001.50',
                             'Currency (Currency conversion to amount)': 'USD',
                             'Currency conversion fee': '-1.50',
                             'Currency (Currency conversion fee)': 'USD'}])
        self.assertEqual(s.txns[0].amount, -800)
        self.assertEqual(s.txns[0].meta['cash_movements'], {'GBP': -800, 'USD': 1000})

    def test_bad_financial_value_is_rejected(self):
        with self.assertRaises(ValueError):
            self.parse_rows([{'Action': 'Deposit', 'Time': '2026-01-01',
                              'Total': 'NaN', 'Currency (Total)': 'GBP'}])

    def test_card_credit_is_a_spending_refund(self):
        self.assertEqual(trading212._amount_and_category('Card credit', 20), (20, 'Shopping'))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = init_db(Path(self.tmp.name) / 'db.sqlite')

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def statement(self, description='Shop', amount=-20, meta=None):
        return ParsedStatement('Bank', 'credit', 'Bank-1234', None, '1234',
                               date(2026, 1, 1), date(2026, 1, 31), 0, 20,
                               [RawTxn(date(2026, 1, 3), description, amount,
                                       ext_id='provider-id', category='Shopping', meta=meta)])

    def test_reimport_updates_parser_metadata_preserves_user_edit_and_counts_inserts(self):
        s = self.statement()
        self.assertEqual(_store_statement(self.conn, s, 'one.pdf')['new_txns'], 1)
        self.conn.execute("UPDATE transactions SET category='Travel',category_source='user',merchant='My label'")
        s.txns[0].meta = {'time': '2026-01-03 12:00:00'}
        s.txns[0].amount = -22
        self.assertEqual(_store_statement(self.conn, s, 'one.pdf')['new_txns'], 0)
        row = self.conn.execute('SELECT * FROM transactions').fetchone()
        self.assertEqual(row['amount'], -22)
        self.assertEqual(row['category'], 'Travel')
        self.assertEqual(row['merchant'], 'My label')
        self.assertIn('source_category', row['meta'])
        self.assertEqual(self.conn.execute('SELECT reconciled FROM statements').fetchone()[0], 0)

    def test_repository_move_does_not_duplicate_statement(self):
        s = self.statement()
        _store_statement(self.conn, s, '../../Data/Bank/one.pdf')
        _store_statement(self.conn, s, '/new/location/Data/Bank/one.pdf')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM statements').fetchone()[0], 1)

    def test_legacy_dedupe_key_migrates_without_reinserting(self):
        s = self.statement()
        s.txns[0].ext_id = None
        _store_statement(self.conn, s, 'one.pdf')
        self.conn.execute("UPDATE transactions SET dedupe_key='old-positional-key'")
        self.assertEqual(_store_statement(self.conn, s, 'one.pdf')['new_txns'], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 1)

    def test_revised_merchant_name_uses_same_running_balance_to_dedupe(self):
        s = self.statement('Wikipedia')
        s.txns[0].ext_id = None
        s.txns[0].balance = 75
        _store_statement(self.conn, s, 'old-period.pdf')
        s.txns[0].description = 'Wikimedia'
        self.assertEqual(_store_statement(self.conn, s, 'new-period.pdf')['new_txns'], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 1)

    def test_distinct_provider_ids_are_never_merged_by_amount_or_balance(self):
        s = self.statement()
        _store_statement(self.conn, s, 'one.csv')
        s.txns[0].ext_id = 'different-provider-id'
        self.assertEqual(_store_statement(self.conn, s, 'two.csv')['new_txns'], 1)

    def test_source_filename_cannot_switch_to_another_account(self):
        s = self.statement()
        _store_statement(self.conn, s, 'one.pdf')
        s.account_id = 'Bank-5678'
        with self.assertRaises(ValueError):
            _store_statement(self.conn, s, 'one.pdf')

    def test_shared_connection_import_does_not_commit_callers_transaction(self):
        s = self.statement()
        module = type('Parser', (), {'parse': staticmethod(lambda _: s)})
        with patch('app.ingest.detect_bank', return_value=('Bank', module)):
            ingest_file('one.pdf', conn=self.conn)
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 0)

    def test_failed_file_rolls_back_only_that_file(self):
        s = self.statement()
        module = type('Parser', (), {'parse': staticmethod(lambda _: s)})
        def failing_store(conn, *_):
            conn.execute("INSERT INTO accounts(id,bank,account_type) VALUES('partial','Bank','credit')")
            raise ValueError('Failed halfway')
        with patch('app.ingest.detect_bank', return_value=('Bank', module)), \
             patch('app.ingest._store_statement', side_effect=failing_store):
            with self.assertRaises(ValueError):
                ingest_file('one.pdf', conn=self.conn)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM accounts').fetchone()[0], 0)

    def test_identical_same_day_charges_preserved_and_overlap_deduplicated(self):
        s = self.statement()
        s.txns[0].ext_id = None
        s.txns.append(RawTxn(date(2026, 1, 3), 'Shop', -20))
        first = _store_statement(self.conn, s, 'one.pdf')
        second = _store_statement(self.conn, s, 'overlap.pdf')
        self.assertEqual(first['new_txns'], 2)
        self.assertEqual(second['new_txns'], 0)

    def test_header_cleanup_keeps_existing_transaction_and_override(self):
        s = self.statement('details type and details Shop')
        s.txns[0].ext_id = None
        _store_statement(self.conn, s, 'one.pdf')
        old_key = self.conn.execute('SELECT dedupe_key FROM transactions').fetchone()[0]
        self.conn.execute("INSERT INTO overrides(dedupe_key,category) VALUES(?, 'Travel')", (old_key,))
        s.txns[0].description = 'Shop'
        self.assertEqual(_store_statement(self.conn, s, 'one.pdf')['new_txns'], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 1)
        new_key = self.conn.execute('SELECT dedupe_key FROM transactions').fetchone()[0]
        self.assertNotEqual(old_key, new_key)
        self.assertEqual(self.conn.execute('SELECT dedupe_key FROM overrides').fetchone()[0], new_key)


if __name__ == '__main__':
    unittest.main()

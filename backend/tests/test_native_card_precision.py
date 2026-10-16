"""Card calculations preserve each supported currency's smallest money unit."""
from datetime import date, datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import account_settings, bank_balance_rules, db, lunchflow_store, wealth


class NativeCardPrecisionTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(folder) / 'synthetic.db'
        self.conn = db.init_db(self.path)
        self.addCleanup(self.conn.close)
        self.clock = self.enterContext(patch.object(bank_balance_rules, 'datetime', wraps=datetime))
        self.clock.now.return_value = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)

    def card(self, currency):
        with self.conn:
            identifier = account_settings.create_manual(self.conn, account_settings.ManualAccount(
                display_name=f'Synthetic {currency} card', account_type='credit', currency=currency))['id']
            self.conn.execute('''INSERT INTO bank_connections(id,account_id,name,institution_name,currency,
                status,raw,first_seen_at,last_seen_at) VALUES(?,?,'Synthetic card','Manual',?,'ACTIVE',
                '{}','2026-10-01T08:00:00+00:00','2026-10-01T08:00:00+00:00')''', (currency, identifier, currency))
        return identifier

    def rule(self, currency, limit):
        return bank_balance_rules.set_rule(self.conn, currency, balance_type='available_credit',
                                            credit_limit=limit, currency=currency)

    def observe(self, currency, amount, stamp='2026-10-01T10:00:00+00:00'):
        lunchflow_store.record_account_result(self.conn, currency, {
            'balance': {'amount': amount, 'currency': currency}, 'transactions': [], 'errors': []}, stamp)

    def test_limits_use_currency_quantum_and_reject_finer_values(self):
        for currency, valid, invalid in (
                ('KWD', 1000.123, 1000.1234),
                ('JPY', 1000, 1000.1),
                ('GBP', 1000.12, 1000.123)):
            with self.subTest(currency=currency):
                self.card(currency)
                with self.conn:
                    saved = self.rule(currency, valid)
                with self.assertRaises(ValueError), self.conn:
                    self.rule(currency, invalid)
                self.assertEqual(bank_balance_rules.get_rule(self.conn, currency), saved)

    def test_live_and_recorded_debt_preserve_native_precision_in_reports(self):
        for currency, limit, available, expected in (
                ('KWD', 1000.120, 998.456, -1.664),
                ('JPY', 1000, 998, -2),
                ('GBP', 1000.12, 998.46, -1.66)):
            with self.subTest(currency=currency):
                account_id = self.card(currency)
                with self.conn:
                    self.rule(currency, limit)
                    self.observe(currency, available)
                self.assertEqual(lunchflow_store.latest_balances(self.conn, today=date(2026, 10, 1))[account_id]['amount'], expected)
                recorded = self.conn.execute('''SELECT effective_amount,currency FROM bank_credit_valuations
                    WHERE connection_id=?''', (currency,)).fetchone()
                self.assertEqual(tuple(recorded), (expected, currency))
                summary = lunchflow_store.summary(self.conn, today=date(2026, 10, 1))
                self.assertEqual(summary['totals_by_currency'][currency], expected)
                self.assertEqual(summary['balance_totals_by_currency'][currency]['liabilities'], -expected)
                institution = next(item for item in summary['institutions'] if currency in item['totals_by_currency'])
                self.assertEqual(institution['totals_by_currency'][currency], expected)
                report = wealth.bundle(self.conn, path=self.path, currency_code=currency,
                                      account_ids=['statement:' + account_id], today=date(2026, 10, 1))
                account = next(item for item in report['accounts'] if item['id'] == 'statement:' + account_id)
                self.assertEqual(account['native_value'], expected)
                self.assertEqual(report['summary']['net_worth'], expected)
                self.assertEqual(report['points'][-1]['net_worth'], expected)

    def test_attested_backfill_preserves_precision_and_original_provider_amount(self):
        for currency, limit, available, expected in (
                ('KWD', 1000.120, 998.456, -1.664),
                ('JPY', 1000, 998, -2),
                ('GBP', 1000.12, 998.46, -1.66)):
            with self.subTest(currency=currency):
                self.card(currency)
                with self.conn:
                    self.observe(currency, available)
                args = dict(start_date='2026-10-01', end_date='2026-10-01',
                            credit_limit=limit, currency=currency, attested_at='2026-10-02T10:00:00+00:00')
                preview = bank_balance_rules.preview_attested_credit_backfill(self.conn, currency, **args)
                self.assertEqual(preview['candidates'][0]['effective_amount'], expected)
                with self.conn:
                    self.assertEqual(bank_balance_rules.apply_attested_credit_backfill(self.conn, currency, **args)['inserted'], 1)
                with self.conn:
                    self.assertEqual(bank_balance_rules.apply_attested_credit_backfill(self.conn, currency, **args)['inserted'], 0)
                self.assertEqual(self.conn.execute('SELECT amount FROM bank_balance_observations WHERE connection_id=?', (currency,)).fetchone()[0], available)
                self.assertEqual(self.conn.execute('SELECT effective_amount FROM bank_credit_valuations WHERE connection_id=?', (currency,)).fetchone()[0], expected)

    def test_previously_recorded_rounded_debt_is_not_rewritten(self):
        self.card('KWD')
        with self.conn:
            self.rule('KWD', 1000.120)
            self.observe('KWD', 998.456)
            self.conn.execute('UPDATE bank_credit_valuations SET effective_amount=-1.66')
        original = tuple(self.conn.execute('SELECT * FROM bank_credit_valuations').fetchone())
        bank_balance_rules.record_credit_valuation(self.conn, 'KWD', original[0])
        self.assertEqual(tuple(self.conn.execute('SELECT * FROM bank_credit_valuations').fetchone()), original)
        with self.conn:
            self.observe('KWD', 998.456, '2026-10-01T11:00:00+00:00')
        self.assertEqual([row[0] for row in self.conn.execute('SELECT effective_amount FROM bank_credit_valuations ORDER BY balance_observation_id')], [-1.66, -1.664])

    def test_native_transaction_totals_keep_three_decimal_units(self):
        self.card('KWD')
        with self.conn:
            self.conn.execute('''INSERT INTO bank_transactions(connection_id,provider_id,txn_date,amount,
                currency,description,status,raw,first_seen_at,last_seen_at)
                VALUES('KWD','purchase','2026-10-01',-1.664,'KWD','Synthetic purchase','native','{}',
                '2026-10-01T10:00:00+00:00','2026-10-01T10:00:00+00:00')''')
        detail = lunchflow_store.detail(self.conn, 'KWD')
        self.assertEqual(detail['totals_by_currency']['KWD']['money_out'], 1.664)

    def test_legacy_fractional_yen_rule_keeps_available_credit_interpretation(self):
        account_id = self.card('JPY')
        with self.conn:
            saved = self.rule('JPY', 1000)
            legacy = saved | {'credit_limit': 1000.12}
            self.conn.execute('UPDATE preferences SET value=? WHERE key=?',
                              (json.dumps(legacy), bank_balance_rules.PREFIX + 'JPY'))
            self.observe('JPY', 998)
        self.assertEqual(bank_balance_rules.get_rule(self.conn, 'JPY'), legacy | {'legacy_fractional_limit': True})
        balance = lunchflow_store.latest_balances(self.conn, today=date(2026, 10, 1))[account_id]
        self.assertEqual(balance['amount'], -2)
        self.assertEqual(balance['valuation']['basis'], 'available_credit')
        self.assertEqual(self.conn.execute('SELECT value FROM preferences WHERE key=?',
                                          (bank_balance_rules.PREFIX + 'JPY',)).fetchone()[0], json.dumps(legacy))
        with self.assertRaises(ValueError), self.conn:
            self.rule('JPY', 1000.12)


if __name__ == '__main__':
    unittest.main()

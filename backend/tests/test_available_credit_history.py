"""A current credit limit cannot establish historic debt from available credit."""
from datetime import date, datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import bank_balance_rules, lunchflow_store, wealth
from app.db import init_db


class AvailableCreditHistoryTests(unittest.TestCase):
    def test_init_db_adds_credit_history_tables_to_an_existing_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy.db'
            old = init_db(path)
            old.execute("INSERT INTO accounts(id,bank,account_type) VALUES('card','Synthetic','credit')")
            old.execute('DROP TABLE bank_credit_valuations')
            old.execute('DROP TABLE bank_credit_rule_events')
            old.commit()
            old.close()
            upgraded = init_db(path)
            try:
                tables = {row[0] for row in upgraded.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertIn('bank_credit_valuations', tables)
                self.assertIn('bank_credit_rule_events', tables)
                self.assertEqual(upgraded.execute("SELECT account_type FROM accounts WHERE id='card'").fetchone()[0], 'credit')
            finally:
                upgraded.close()

    def test_recorded_credit_value_survives_day_rollover_and_later_rule_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = init_db(Path(directory) / 'history.db')
            self.addCleanup(conn.close)
            lunchflow_store.ensure_schema(conn)
            conn.execute("INSERT INTO accounts(id,bank,account_type) VALUES('card','Synthetic','credit')")
            conn.execute("""INSERT INTO bank_connections(id,account_id,name,institution_name,
                currency,status,balance_multiplier,raw,first_seen_at,last_seen_at)
                VALUES('feed','card','card','Synthetic','GBP','ACTIVE',-1,'{}',
                       '2026-08-01T00:00:00+00:00','2026-10-03T00:00:00+00:00')""")
            conn.execute("""INSERT INTO statements(account_id,source_file,period_end,closing_balance,reconciled)
                VALUES('card','synthetic','2026-08-16',100,1)""")
            conn.execute("""INSERT INTO bank_balance_observations(connection_id,amount,currency,fetched_at)
                VALUES('feed',900,'GBP','2026-09-17T12:00:00+00:00')""")
            with patch.object(bank_balance_rules, 'datetime', wraps=datetime) as clock:
                clock.now.return_value = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
                bank_balance_rules.set_rule(conn, 'feed', balance_type='available_credit',
                    credit_limit=1000, currency='GBP')
                lunchflow_store.record_account_result(conn, 'feed', {
                    'balance': {'amount':800, 'currency':'GBP'}, 'transactions':[], 'errors':[]},
                    '2026-10-01T21:00:00+00:00')
                with patch.object(wealth.snaptrade_store, 'load_portfolio', return_value=None), \
                     patch.object(wealth.snaptrade_store, 'list_account_observations', return_value=[]), \
                     patch.object(wealth.snaptrade_store, 'list_account_estimates', return_value=[]):
                    on_day = wealth.bundle(conn, today=date(2026, 10, 1))
                    after_rollover = wealth.bundle(conn, today=date(2026, 10, 2))
                self.assertEqual(on_day['summary']['net_worth'], -200)
                oct1 = next(point for point in after_rollover['history']['points']
                    if point['date'] == '2026-10-01')
                self.assertTrue(oct1['complete'])
                self.assertEqual(oct1['net_worth'], -200)
                self.assertFalse(oct1['estimated'])
                clock.now.return_value = datetime(2026, 10, 2, 9, tzinfo=timezone.utc)
                bank_balance_rules.set_rule(conn, 'feed', balance_type='available_credit',
                    credit_limit=1200, currency='GBP')
                lunchflow_store.record_account_result(conn, 'feed', {
                    'balance': {'amount':760, 'currency':'GBP'}, 'transactions':[], 'errors':[]},
                    '2026-10-02T12:00:00+00:00')
                clock.now.return_value = datetime(2026, 10, 3, 9, tzinfo=timezone.utc)
                bank_balance_rules.set_rule(conn, 'feed', balance_type='reported')
                lunchflow_store.record_account_result(conn, 'feed', {
                    'balance': {'amount':400, 'currency':'GBP'}, 'transactions':[], 'errors':[]},
                    '2026-10-03T12:00:00+00:00')
            with patch.object(wealth.snaptrade_store, 'load_portfolio', return_value=None), \
                 patch.object(wealth.snaptrade_store, 'list_account_observations', return_value=[]), \
                 patch.object(wealth.snaptrade_store, 'list_account_estimates', return_value=[]):
                later = wealth.bundle(conn, today=date(2026, 10, 4))
            points = {point['date']:point for point in later['history']['points']}
            self.assertEqual(points['2026-09-17']['net_worth'], -100)
            self.assertEqual([points[day]['net_worth'] for day in
                ('2026-10-01','2026-10-02','2026-10-03')], [-200,-440,-400])
            self.assertTrue(all(points[day]['complete'] for day in
                ('2026-10-01','2026-10-02','2026-10-03')))
            saved = lunchflow_store.credit_valuation_history(conn, 'card')
            self.assertEqual([(row['effective_amount'],row['credit_limit'],row['balance_type'])
                for row in saved], [(-200,1000,'available_credit'),(-440,1200,'available_credit'),
                                   (-400,None,'reported')])
            conn.execute("UPDATE bank_connections SET account_id=NULL,present=0 WHERE id='feed'")
            with patch.object(wealth.snaptrade_store, 'load_portfolio', return_value=None), \
                 patch.object(wealth.snaptrade_store, 'list_account_observations', return_value=[]), \
                 patch.object(wealth.snaptrade_store, 'list_account_estimates', return_value=[]):
                disconnected = wealth.bundle(conn, today=date(2026, 10, 4),
                    account_ids=['statement:card'])
            retained = {point['date']:point for point in disconnected['history']['points']}
            self.assertEqual([retained[day]['net_worth'] for day in
                ('2026-10-01','2026-10-02','2026-10-03')], [-200,-440,-400])

    def test_dated_attestation_is_previewed_and_retains_source_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = init_db(Path(directory) / 'history.db')
            self.addCleanup(conn.close)
            lunchflow_store.ensure_schema(conn)
            conn.execute("INSERT INTO accounts(id,bank,account_type) VALUES('card','Synthetic','credit')")
            conn.execute("""INSERT INTO bank_connections(id,account_id,name,institution_name,
                currency,status,balance_multiplier,raw,first_seen_at,last_seen_at)
                VALUES('feed','card','card','Synthetic','GBP','ACTIVE',-1,'{}',
                       '2026-09-01T00:00:00+00:00','2026-10-02T00:00:00+00:00')""")
            conn.execute("""INSERT INTO bank_balance_observations(connection_id,amount,currency,fetched_at)
                VALUES('feed',7733.91,'GBP','2026-10-01T21:58:48+00:00')""")
            args = dict(start_date='2026-10-01', end_date='2026-10-01', credit_limit=8750,
                currency='GBP', attested_at='2026-10-02T15:00:00+00:00')
            before = conn.total_changes
            preview = bank_balance_rules.preview_attested_credit_backfill(conn, 'feed', **args)
            self.assertEqual(conn.total_changes, before)
            self.assertEqual(preview['source_balances'], 1)
            self.assertEqual(preview['candidates'][0]['effective_amount'], -1016.09)
            self.assertEqual(preview['candidates'][0]['scope_end'], '2026-10-01')
            applied = bank_balance_rules.apply_attested_credit_backfill(conn, 'feed', **args)
            self.assertEqual(applied['inserted'], 1)
            self.assertEqual(bank_balance_rules.apply_attested_credit_backfill(conn, 'feed', **args)['inserted'], 0)
            saved = lunchflow_store.credit_valuation_history(conn, 'card')[0]
            self.assertEqual(saved['evidence_kind'], 'retrospective_attestation')
            self.assertEqual(saved['estimated'], 1)
            self.assertEqual(saved['effective_amount'], -1016.09)
            self.assertEqual(conn.execute('SELECT amount FROM bank_balance_observations').fetchone()[0], 7733.91)
            with patch.object(wealth.snaptrade_store, 'load_portfolio', return_value=None), \
                 patch.object(wealth.snaptrade_store, 'list_account_observations', return_value=[]), \
                 patch.object(wealth.snaptrade_store, 'list_account_estimates', return_value=[]):
                history = wealth.bundle(conn, today=date(2026, 10, 2))['history']['points']
            oct1 = next(point for point in history if point['date'] == '2026-10-01')
            self.assertEqual(oct1['net_worth'], -1016.09)
            self.assertTrue(oct1['estimated'])
            with self.assertRaises(ValueError):
                bank_balance_rules.preview_attested_credit_backfill(conn, 'feed', **(args | {'currency':'USD'}))

    def test_current_limit_does_not_rewrite_provider_history_or_statement_debt(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = init_db(Path(directory) / 'history.db')
            self.addCleanup(conn.close)
            lunchflow_store.ensure_schema(conn)
            for name, kind, sign in [('card', 'credit', -1), ('cash', 'current', 1)]:
                conn.execute('INSERT INTO accounts(id,bank,account_type) VALUES(?,?,?)', (name, 'Synthetic', kind))
                conn.execute('''INSERT INTO bank_connections(id,account_id,name,institution_name,
                    currency,status,balance_multiplier,raw,first_seen_at,last_seen_at)
                    VALUES(?,?,?,'Synthetic','GBP','ACTIVE',?,'{}','2026-09-01','2026-09-18')''',
                    (name, name, name, sign))
            conn.execute('''INSERT INTO statements(account_id,source_file,period_end,closing_balance,reconciled)
                VALUES('card','synthetic','2026-09-01',50,1)''')
            for when, account, amount in [('2026-09-01', 'cash', 500),
                    ('2026-09-17', 'cash', 500), ('2026-09-17', 'card', 900),
                    ('2026-09-18', 'cash', 500), ('2026-09-18', 'card', 800)]:
                conn.execute('''INSERT INTO bank_balance_observations(connection_id,amount,currency,fetched_at)
                    VALUES(?,?,'GBP',?)''', (account, amount, when + 'T08:00:00+00:00'))
            bank_balance_rules.set_rule(conn, 'card', balance_type='available_credit', credit_limit=1000, currency='GBP')
            with patch.object(wealth.snaptrade_store, 'load_portfolio', return_value=None), \
                    patch.object(wealth.snaptrade_store, 'list_account_observations', return_value=[]), \
                    patch.object(wealth.snaptrade_store, 'list_account_estimates', return_value=[]):
                before = conn.total_changes
                result = wealth.bundle(conn, today=date(2026, 9, 18))
                self.assertEqual(conn.total_changes, before)
                self.assertEqual(result['summary']['net_worth'], 300)
                points = {point['date']: point for point in result['points']}
                self.assertEqual(points['2026-09-01']['liabilities'], 50)
                self.assertEqual(points['2026-09-17']['net_worth'], 450)
                self.assertEqual(points['2026-09-17']['missing_accounts'], 0)
                self.assertEqual(points['2026-09-17']['carried_accounts'], 1)
                self.assertTrue(points['2026-09-17']['estimated'])
                self.assertEqual(result['history']['start_date'], '2026-09-01')
                self.assertEqual(next(point for point in result['history']['points']
                    if point['date'] == '2026-09-17')['net_worth'], 450)
                self.assertFalse(result['history']['change_comparable'])
                self.assertNotEqual(points['2026-09-17']['valuation_key'],
                                    points['2026-09-18']['valuation_key'])
                self.assertEqual(points['2026-09-18']['liabilities'], 200)
                bank_balance_rules.set_rule(conn, 'card', balance_type='available_credit', credit_limit=2000, currency='GBP')
                changed = wealth.bundle(conn, today=date(2026, 9, 18))
                self.assertEqual(changed['summary']['liabilities'], 1200)
                self.assertEqual(changed['points'][:-1], result['points'][:-1])
                self.assertEqual([row[0] for row in conn.execute(
                    "SELECT amount FROM bank_balance_observations WHERE connection_id='card' ORDER BY fetched_at")], [900, 800])
                conn.execute('''INSERT INTO statements(account_id,source_file,period_end,closing_balance,reconciled)
                    VALUES('card','newer-synthetic','2026-09-19',300,1)''')
                newer = wealth.bundle(conn, today=date(2026, 9, 19))
                self.assertEqual(newer['summary']['liabilities'], 300)
                latest_points = {point['date']: point for point in newer['points']}
                self.assertEqual(latest_points['2026-09-18']['net_worth'], 450)
                self.assertEqual(latest_points['2026-09-18']['missing_accounts'], 0)

    def test_same_day_available_credit_does_not_erase_reconciled_debt_anchor(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = init_db(Path(directory) / 'history.db')
            self.addCleanup(conn.close)
            lunchflow_store.ensure_schema(conn)
            conn.execute("INSERT INTO accounts(id,bank,account_type) VALUES('card','Synthetic','credit')")
            conn.execute("""INSERT INTO bank_connections(id,account_id,name,institution_name,
                currency,status,balance_multiplier,raw,first_seen_at,last_seen_at)
                VALUES('card','card','card','Synthetic','GBP','ACTIVE',-1,'{}','2026-09-01','2026-10-20')""")
            conn.execute("""INSERT INTO statements(account_id,source_file,period_end,closing_balance,reconciled)
                VALUES('card','synthetic','2026-09-01',50,1)""")
            for when in ('2026-09-01', '2026-09-17', '2026-10-20'):
                conn.execute("""INSERT INTO bank_balance_observations(connection_id,amount,currency,fetched_at)
                    VALUES('card',900,'GBP',?)""", (when + 'T08:00:00+00:00',))
            bank_balance_rules.set_rule(conn, 'card', balance_type='available_credit',
                                      credit_limit=1000, currency='GBP')
            with patch.object(wealth.snaptrade_store, 'load_portfolio', return_value=None), \
                    patch.object(wealth.snaptrade_store, 'list_account_observations', return_value=[]), \
                    patch.object(wealth.snaptrade_store, 'list_account_estimates', return_value=[]):
                result = wealth.bundle(conn, today=date(2026, 10, 20))
            points = {point['date']: point for point in result['history']['points']}
            self.assertEqual(points['2026-09-01']['net_worth'], -50)
            self.assertEqual(points['2026-09-17']['net_worth'], -50)
            self.assertIsNone(points['2026-10-18']['net_worth'])
            self.assertEqual(points['2026-10-20']['net_worth'], -100)


if __name__ == '__main__':
    unittest.main()

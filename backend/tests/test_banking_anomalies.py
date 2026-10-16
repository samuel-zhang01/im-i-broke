"""Causal, explainable bank evidence; reviewed outcomes and investment parity."""
import json
import sqlite3
import unittest
from datetime import date,timedelta

from app import analytics
from app.anomaly_learning import detect
from app.db import SCHEMA


class BankEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:');self.conn.row_factory=sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.executemany('INSERT INTO accounts(id,bank,account_type,display_name) VALUES(?,?,?,?)',
            [('bank','Bank','current','Bank'),('card','Bank','credit','Card'),('broker','Broker','investment','Broker')])
        self.addCleanup(self.conn.close)

    def txn(self,day,amount=-10,merchant='Shop',account='bank',category='Shopping',kind='merchant',code='CARD'):
        ident=self.conn.execute('INSERT INTO transactions(account_id,txn_date,description,merchant,source_merchant,merchant_kind,amount,category,type_code) VALUES(?,?,?,?,?,?,?,?,?)',
            (account,day,merchant,merchant,merchant,kind,amount,category,code)).lastrowid
        self.conn.execute('UPDATE transactions SET dedupe_key=? WHERE id=?',(str(ident),ident))
        return ident

    def history(self,merchant='Shop',account='bank',category='Shopping',amount=-10):
        return [self.txn(f'2026-01-{i+1:02d}',amount,merchant,account,category) for i in range(5)]

    def candidates(self):
        return analytics.detect_anomalies(self.conn,include_resolved=True)

    def test_future_and_same_day_payments_never_establish_amount_baseline(self):
        target=self.txn('2026-01-01',-1000)
        for i in range(10):self.txn(f'2026-02-{i+1:02d}',-10)
        self.assertFalse(any(row['id']==target for row in self.candidates()))
        for i in range(5):self.txn('2025-12-01',-10,merchant='Other')
        same_day=self.txn('2025-12-01',-1000,merchant='Other')
        self.assertFalse(any(row['id']==same_day and row['kind']=='large_amount' for row in self.candidates()))

    def test_known_spike_has_inspectable_strictly_prior_evidence(self):
        history=self.history();target=self.txn('2026-01-20',-1000)
        flag=next(row for row in self.candidates() if row['id']==target)
        self.assertEqual(flag['kind'],'large_amount');self.assertEqual(flag['priority'],'high')
        self.assertEqual(flag['history_count'],5);self.assertEqual(flag['comparison_transaction_ids'],history)
        self.assertEqual(flag['baseline_amount'],10);self.assertLess(flag['baseline_to'],flag['txn_date'])
        # Later unusual values cannot retroactively redefine this decision.
        self.txn('2026-02-20',-5000)
        self.assertEqual(flag,next(row for row in self.candidates() if row['id']==target))

    def test_other_account_and_payment_context_do_not_define_rent_baseline(self):
        self.history('Housing provider','card','Housing')
        rent=self.txn('2026-01-20',-1600,'Housing provider',category='Housing',code='BP')
        self.assertFalse(any(row['id']==rent for row in self.candidates()))
        self.history('Housing provider','bank','Housing')
        self.assertFalse(any(row['id']==rent for row in self.candidates()))

    def test_old_history_expires_after_one_year(self):
        for i in range(5):self.txn(f'2024-01-{i+1:02d}',-10)
        target=self.txn('2026-01-01',-1000)
        self.assertFalse(any(row['id']==target for row in self.candidates()))

    def test_routine_repeats_remain_inspectable_with_low_priority(self):
        first=self.txn('2026-01-01',-2.8,'Transit',category='Transport')
        second=self.txn('2026-01-02',-2.8,'Transit',category='Transport')
        flag=next(row for row in self.candidates() if row['id']==second)
        self.assertEqual(flag['priority'],'low');self.assertEqual(flag['comparison_transaction_ids'],[first])
        self.assertEqual(analytics.monthly_cashflow(self.conn,exclude_outliers=True)[0]['spend'],5.6)
        first=self.txn('2026-02-01',-300,'Equipment')
        second=self.txn('2026-02-01',-300,'Equipment')
        flag=next(row for row in self.candidates() if row['id']==second)
        self.assertEqual(flag['priority'],'high');self.assertIn(first,flag['comparison_transaction_ids'])

    def test_current_explicit_feedback_still_wins_and_bad_snapshot_never_teaches(self):
        self.history();target=self.txn('2026-01-20',-1000)
        self.conn.execute('INSERT INTO anomaly_reviews(dedupe_key,outcome,snapshot) VALUES(?,?,?)',(str(target),'expected','not-json'))
        self.assertNotIn(target,[row['id'] for row in analytics.detect_anomalies(self.conn)])
        other=self.txn('2026-01-25',-1000)
        self.assertIn(other,[row['id'] for row in analytics.detect_anomalies(self.conn)])

    def test_investment_flags_keep_legacy_inputs_and_exact_outputs(self):
        for i in range(5):self.txn(f'2026-01-{i+1:02d}',-10,'Broker shop','broker')
        self.txn('2026-01-20',-1000,'Broker shop','broker')
        self.history()
        expected=[row for row in analytics._legacy_anomalies(self.conn,include_resolved=True) if row['account_type']=='investment']
        actual=[row for row in self.candidates() if row['account_type']=='investment']
        self.assertEqual(actual,expected)

    def test_recurring_separates_bills_from_habits_and_variable_fees(self):
        for day in ['2026-01-01','2026-02-01','2026-03-01']:
            self.txn(day,-10,'Stream',category='Subscriptions')
            self.txn(day,-8,'Favourite cafe',category='Eating out')
            self.txn(day,-1,'Foreign card fee',category='Fees & interest')
        rows={row['merchant']:row for row in analytics.detect_recurring(self.conn,as_of=date(2026,3,2))}
        self.assertTrue(rows['Stream']['forecast_eligible'])
        self.assertEqual(rows['Favourite cafe']['pattern_type'],'spending_habit')
        self.assertFalse(rows['Favourite cafe']['forecast_eligible'])
        self.assertEqual(rows['Foreign card fee']['pattern_type'],'variable_cost')
        self.assertFalse(rows['Foreign card fee']['forecast_eligible'])
        self.assertGreater(rows['Favourite cafe']['monthly_cost'],0)

    def test_recurring_as_of_ignores_future_bank_payments(self):
        for day in ['2026-01-01','2026-02-01','2026-03-01']:
            self.txn(day,-10,'Stream',category='Subscriptions')
        self.assertEqual(analytics.detect_recurring(self.conn,as_of=date(2026,2,15)),[])

    def test_recurring_as_of_retains_earlier_statement_coverage(self):
        for day in ['2026-01-01','2026-02-01','2026-03-01']:
            self.txn(day,-10,'Stream',category='Subscriptions')
        self.conn.executemany('INSERT INTO statements(account_id,source_file,period_end) VALUES(?,?,?)',
                              [('bank','earlier.pdf','2026-03-20'),('bank','future.pdf','2026-05-20')])
        row=analytics.detect_recurring(self.conn,as_of=date(2026,3,21))[0]
        self.assertEqual(row['observed_through'],'2026-03-20')

    def test_bank_marker_refresh_preserves_sources_investments_and_caller_transaction(self):
        self.history();target=self.txn('2026-01-20',-1000)
        broker=self.txn('2026-01-20',-20,'Broker shop','broker')
        self.conn.execute('UPDATE transactions SET is_anomaly=1,transfer_group=? WHERE id=?',('unchanged',broker))
        self.conn.commit()
        before={row['id']:dict(row) for row in self.conn.execute('SELECT * FROM transactions')}
        result=analytics.refresh_bank_anomaly_flags(self.conn)
        self.assertGreater(result['changed'],0)
        self.assertTrue(self.conn.in_transaction)
        after={row['id']:dict(row) for row in self.conn.execute('SELECT * FROM transactions')}
        self.assertEqual(before[broker],after[broker])
        for ident,row in before.items():
            self.assertEqual({key:value for key,value in row.items() if key!='is_anomaly'},
                             {key:value for key,value in after[ident].items() if key!='is_anomaly'})
        self.assertEqual(after[target]['is_anomaly'],1)
        self.assertEqual(analytics.refresh_bank_anomaly_flags(self.conn)['changed'],0)
        self.conn.rollback()
        self.assertEqual(before,{row['id']:dict(row) for row in self.conn.execute('SELECT * FROM transactions')})

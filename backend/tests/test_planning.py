import calendar
import tempfile
import unittest
from datetime import date
from pathlib import Path
from app.db import init_db
from app.planning import (compound_scenario, covered_baseline, account_snapshot, planning_bundle,
                          save_scenario, saved_scenarios, delete_scenario)


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conn = init_db(Path(self.temp.name)/'test.db')
        self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('bank','Test','current','Test bank')")

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def statement(self, start, end, opening=0, closing=1000, rec=1):
        self.conn.execute('INSERT INTO statements(account_id,source_file,period_start,period_end,opening_balance,closing_balance,reconciled) VALUES(?,?,?,?,?,?,?)', ('bank',start,start,end,opening,closing,rec))

    def txn(self, d, amount, category='Income'):
        self.conn.execute("INSERT INTO transactions(account_id,txn_date,description,amount,category) VALUES('bank',?,'Test',?,?)", (d,amount,category))

    def test_zero_return_and_end_month_contributions(self):
        r = compound_scenario(1000,100,0,0,1,2000)
        self.assertEqual(r['points'][-1]['nominal'],2200)
        self.assertEqual(r['target_month'],10)

    def test_effective_annual_return_and_inflation(self):
        r = compound_scenario(1000,0,12,12,1,2000)
        self.assertEqual(r['points'][-1]['nominal'],1120)
        self.assertEqual(r['points'][-1]['real'],1000)

    def test_deficit_is_not_clamped_and_unreachable_goal(self):
        r = compound_scenario(100,-20,0,0,1,200)
        self.assertEqual(r['points'][-1]['nominal'],-140)
        self.assertIsNone(r['target_month'])

    def test_no_income_savings_rate_is_undefined(self):
        self.statement('2026-01-01','2026-03-31')
        self.txn('2026-02-01',-100,'Groceries')
        result = covered_baseline(self.conn,date(2026,4,5))
        self.assertEqual(result['months'],3)
        self.assertIsNone(result['savings_rate'])
        self.assertEqual(result['avg_spending'],33.33)

    def test_gap_and_partial_month_are_excluded(self):
        self.statement('2026-01-01','2026-01-15')
        self.statement('2026-01-17','2026-02-28')
        self.statement('2026-03-01','2026-03-15')
        self.txn('2026-02-01',1000)
        self.txn('2026-02-03',-300,'Groceries')
        result = covered_baseline(self.conn,date(2026,3,20))
        self.assertEqual([m['month'] for m in result['monthly']],['2026-02'])
        self.assertEqual(result['avg_surplus'],700)

    def test_failed_statements_do_not_support_baseline(self):
        self.statement('2026-01-01','2026-01-31',rec=0)
        self.assertEqual(covered_baseline(self.conn,date(2026,2,5))['months'],0)

    def test_unknown_balance_is_not_zero(self):
        self.statement('2026-01-01','2026-01-31',closing=None,rec=None)
        result = account_snapshot(self.conn,date(2026,2,5))
        self.assertIsNone(result[0]['balance'])
        self.assertIsNone(result[0]['reconciled'])

    def test_credit_balance_is_liability_and_overpayment_asset(self):
        self.conn.execute("UPDATE accounts SET account_type='credit'")
        self.statement('2026-01-01','2026-01-31',closing=250)
        self.assertEqual(account_snapshot(self.conn)[0]['balance'],-250)
        self.conn.execute("UPDATE statements SET closing_balance=-10")
        self.assertEqual(account_snapshot(self.conn)[0]['balance'],10)

    def test_absent_baseline_requires_manual_contribution(self):
        r = planning_bundle(self.conn,today=date(2026,2,5))
        self.assertEqual(r['scenarios'],[])
        self.assertIsNone(r['assumptions']['monthly_contribution'])
        self.assertEqual(len(planning_bundle(self.conn,monthly_contribution=100)['scenarios']),3)

    def test_investment_income_not_counted_again_as_contribution(self):
        self.statement('2026-01-01','2026-01-31')
        self.txn('2026-01-02',1000)
        self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('invest','Invest','investment','Invest')")
        self.conn.execute("INSERT INTO transactions(account_id,txn_date,description,amount,category) VALUES('invest','2026-01-02','Interest',100,'Investment income')")
        r=covered_baseline(self.conn,date(2026,2,5))
        self.assertEqual(r['avg_income'],1000)

    def test_nonfinite_input_rejected(self):
        with self.assertRaises(ValueError):
            compound_scenario(float('nan'),0,0,0,1,100)

    def test_median_is_computed_from_monthly_surplus(self):
        self.statement('2026-01-01', '2026-03-31')
        for month, income, spending in [('01', 1000, 900), ('02', 2000, 1000), ('03', 10000, 9900)]:
            self.txn(f'2026-{month}-05', income)
            self.txn(f'2026-{month}-06', -spending, 'Shopping')
        result = planning_bundle(self.conn, today=date(2026, 4, 5), baseline_method='median')
        self.assertEqual(result['baseline']['median_surplus'], 100)
        self.assertEqual(result['baseline']['avg_surplus'], 400)
        self.assertEqual(result['assumptions']['monthly_contribution'], 100)
        self.assertNotEqual(result['baseline']['median_income']-result['baseline']['median_spending'], 100)

    def test_outlier_adjustment_does_not_shrink_cash_buffer(self):
        # Five genuinely earlier merchant payments establish this baseline;
        # the extra prior-month payment is outside January's covered budget.
        self.txn('2025-12-20',-10,'Shopping')
        self.statement('2026-01-01', '2026-01-31')
        self.txn('2026-01-01', 2000)
        for i, amount in enumerate([-10, -10, -10, -10, -1000]):
            self.txn(f'2026-01-{2+i*4:02d}', amount, 'Shopping')
        self.conn.execute("UPDATE transactions SET merchant='Same shop' WHERE amount<0")
        result = planning_bundle(self.conn, today=date(2026, 2, 5), exclude_outliers=True)
        self.assertEqual(result['baseline']['avg_spending'], 40)
        self.assertEqual(result['baseline']['recorded_avg_spending'], 1040)
        self.assertEqual(result['baseline']['excluded_outlier_spend'], 1000)
        self.assertEqual(result['assumptions']['monthly_contribution'], 1960)
        self.assertEqual(result['resilience']['three_month_buffer'], 3120)

    def test_planned_expense_is_withdrawn_once_after_contribution(self):
        result = compound_scenario(1000, 100, 0, 0, 2, 2100, one_off_amount=500, one_off_month=12)
        self.assertEqual(result['points'][1]['nominal'], 1700)
        self.assertEqual(result['points'][2]['nominal'], 2900)
        self.assertEqual(result['points'][2]['contributed'], 2900)
        self.assertEqual(result['points'][2]['planned_expenses'], 500)
        self.assertEqual(result['target_month'], 11)
        self.assertTrue(result['ending_above_target'])
        with self.assertRaises(ValueError):
            compound_scenario(1000, 100, 0, 0, 1, 2000, one_off_amount=50, one_off_month=13)

    def test_saved_scenario_roundtrip_update_delete_and_validation(self):
        params = {'years': 2, 'baseline_method': 'median', 'exclude_outliers': True,
                  'one_off_amount': 500, 'one_off_month': 12, 'monthly_contribution': -100}
        created = save_scenario(self.conn, ' My plan ', params)
        self.assertEqual(created['name'], 'My plan')
        self.assertEqual(created['parameters']['monthly_contribution'], -100)
        self.assertEqual(created['parameters']['baseline_method'], 'median')
        changed = save_scenario(self.conn, 'Updated', params, created['id'])
        self.assertEqual(changed['created_at'], created['created_at'])
        self.assertEqual(len(saved_scenarios(self.conn)), 1)
        with self.assertRaises(ValueError):
            save_scenario(self.conn, 'Invalid', {'annual_return': float('nan')})
        with self.assertRaises(ValueError):
            save_scenario(self.conn, 'Invalid', {'years': 1, 'one_off_month': 13})
        with self.assertRaises(ValueError):
            save_scenario(self.conn, '', params)
        self.assertTrue(delete_scenario(self.conn, created['id']))
        self.assertFalse(delete_scenario(self.conn, created['id']))
        self.assertEqual(saved_scenarios(self.conn), [])

    def test_untracked_foreign_currency_keeps_wealth_partial_and_fee_in_baseline(self):
        self.statement('2026-01-01', '2026-01-31')
        self.conn.execute("INSERT INTO transactions(account_id,txn_date,description,amount,category,spending_treatment,meta) VALUES('bank','2026-01-10','Exchanged to EUR',-84.42,'Transfers & payments','self_transfer',?)",
                          ('{"currency_exchange":true,"exchange_fee_gbp":0.84}',))
        result = planning_bundle(self.conn, today=date(2026, 2, 1))
        self.assertEqual(result['baseline']['avg_spending'], .84)
        self.assertTrue(result['wealth']['is_partial'])
        self.assertTrue(result['wealth']['foreign_currency_movements_unvalued'])

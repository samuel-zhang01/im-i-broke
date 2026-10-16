import unittest
import sqlite3
from app.db import SCHEMA
from app.spending_explorer import explore
from app.analytics import analytics_bundle

class ExplorerTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row;self.c.executescript(SCHEMA)
        self.c.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('a','Bank','credit','Card'),('b','Bank','current','Bank')")
        for aid,day,amount,cat,name in [('a','2026-01-10',-100,'Shopping','Shop'),('a','2026-01-12',25,'Shopping','Shop refund'),('a','2026-02-10',-40,'Groceries','Food'),('b','2026-02-12',-20,'Groceries','Food'),('a','2026-02-12',-500,'Transfers & payments','Own transfer')]:
            self.c.execute('INSERT INTO transactions(account_id,txn_date,amount,category,merchant,description) VALUES(?,?,?,?,?,?)',(aid,day,amount,cat,name,name))
    def tearDown(self):self.c.close()
    def test_category_vendor_month_cube_reconciles_to_spending(self):
        for query in [{},{'account':'a'},{'date_from':'2026-02-01','date_to':'2026-02-28'},{'exclude_outliers':True}]:
            x=explore(self.c,**query);a=analytics_bundle(self.c,**query)
            self.assertAlmostEqual(sum(c['net'] for c in x['cells']),a['stats']['total_spend'])
            self.assertAlmostEqual(sum(c['gross']-c['refunds'] for c in x['cells']),sum(c['net'] for c in x['cells']))
        self.assertEqual(explore(self.c)['months'],['2026-01','2026-02'])
        self.assertFalse(any(c['merchant']=='Own transfer' for c in explore(self.c)['cells']))
    def test_empty_months_and_invalid_ranges(self):
        x=explore(self.c,date_from='2026-03-01',date_to='2026-04-30')
        self.assertEqual(x['cells'],[]);self.assertEqual(x['months'],['2026-03','2026-04'])
        with self.assertRaises(ValueError):explore(self.c,date_from='2026-03-01',date_to='2026-01-01')
        with self.assertRaises(ValueError):explore(self.c,date_from='1800-01-01',date_to='2026-01-01')

    def test_excluded_activity_does_not_extend_household_month_range(self):
        for day, category in [('2024-01-01', 'Investments'), ('2027-12-01', 'Transfers & payments')]:
            self.c.execute("INSERT INTO transactions(account_id,txn_date,amount,category,description) VALUES('a',?,-500,?,'Excluded activity')", (day, category))
        self.assertEqual(explore(self.c)['months'], [row['m'] for row in analytics_bundle(self.c)['monthly']])
        self.assertEqual(explore(self.c)['months'], ['2026-01', '2026-02'])
        # Explicit selections still include all requested empty months.
        self.assertEqual(len(explore(self.c, date_from='2026-01-01', date_to='2026-12-31')['months']), 12)

    def test_final_calendar_month_is_finite(self):
        result = explore(self.c, date_from='9999-12-01', date_to='9999-12-31')
        self.assertEqual(result['months'], ['9999-12'])
        self.assertEqual(result['cells'], [])

    def test_weekly_totals_reconcile_under_all_filters(self):
        for query in [{}, {'account': 'a'}, {'date_from': '2026-01-11', 'date_to': '2026-02-11'}, {'exclude_outliers': True}]:
            result = explore(self.c, **query)
            for metric in ('net', 'gross', 'refunds', 'n'):
                self.assertAlmostEqual(sum(c[metric] for c in result['weekly_cells']), sum(c[metric] for c in result['cells']))
            self.assertFalse(any(c['merchant'] == 'Own transfer' for c in result['weekly_cells']))
        result = explore(self.c)
        self.assertEqual(result['weeks'][0], '2026-01-05')
        self.assertEqual(result['weeks'][-1], '2026-02-09')
        self.assertEqual(next(c['week'] for c in result['weekly_cells'] if c['merchant'] == 'Shop refund'), '2026-01-12')
        self.assertFalse(result['weekly_coverage']['2026-01-05']['coverage_complete'])

    def test_weeks_cross_month_and_year_boundaries_without_splitting(self):
        for day in ['2025-12-31', '2026-01-01', '2026-01-04', '2026-01-05']:
            self.c.execute("INSERT INTO transactions(account_id,txn_date,amount,category,merchant,description) VALUES('a',?,-10,'Food','Cafe','Cafe')", (day,))
        result = explore(self.c, date_from='2025-12-31', date_to='2026-01-05')
        self.assertEqual(result['weeks'], ['2025-12-29', '2026-01-05'])
        self.assertEqual([c['net'] for c in result['weekly_cells']], [30, 10])
        self.assertFalse(any(c['coverage_complete'] for c in result['weekly_coverage'].values()))
        empty = explore(self.c, date_from='2026-03-01', date_to='2026-03-09')
        self.assertEqual(empty['weeks'], ['2026-02-23', '2026-03-02', '2026-03-09'])
        self.assertEqual(empty['weekly_cells'], [])

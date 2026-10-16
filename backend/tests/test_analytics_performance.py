"""Keep interactive reads proportional to ledger size without stale results."""
import sqlite3
import statistics
import unittest
from datetime import date,timedelta
from unittest.mock import patch

from app import analytics,planning,recurring_views
from app.db import SCHEMA


class AnalyticsReadEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:')
        self.conn.row_factory=sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('bank','Bank','current','Bank account')")
        for merchant,amount in [('Coffee',-10),('Groceries',-20)]:
            for i in range(100):
                day=(date(2018,1,1)+timedelta(days=31*i)).isoformat()
                self.conn.execute('INSERT INTO transactions(account_id,txn_date,description,merchant,amount,category) VALUES(?,?,?,?,?,?)',
                                  ('bank',day,merchant,merchant,amount,'Shopping'))
        self.conn.execute("UPDATE transactions SET amount=-200 WHERE id=100")
        self.addCleanup(self.conn.close)

    def test_causal_peer_statistics_are_bounded_per_group_and_day(self):
        with patch.object(analytics.statistics,'median',wraps=statistics.median) as median:
            flagged=analytics.detect_anomalies(self.conn,include_resolved=True)
        self.assertEqual([row['id'] for row in flagged if row['kind']=='large_amount'],[100])
        self.assertLessEqual(median.call_count,400)
        # The comparison uses the preceding year, not all future/imported rows.
        self.assertEqual(flagged[0]['baseline_amount'],10)
        self.assertEqual(flagged[0]['history_count'],11)

    def test_analytics_snapshot_reads_once_and_observes_next_edit(self):
        with patch.object(analytics,'_rows',wraps=analytics._rows) as read:
            before=analytics.analytics_bundle(self.conn,exclude_outliers=True)
        self.assertEqual(read.call_count,1)
        self.assertEqual(before['stats']['excluded_outlier_count'],1)
        self.conn.execute("UPDATE transactions SET amount=-10 WHERE id=100")
        after=analytics.analytics_bundle(self.conn,exclude_outliers=True)
        self.assertEqual(after['stats']['excluded_outlier_count'],0)
        self.assertEqual(after['stats']['total_spend'],3000)

    def test_planning_reuses_one_ledger_snapshot_with_same_results(self):
        with patch.object(analytics,'_rows',wraps=analytics._rows) as read:
            result=planning.planning_bundle(self.conn,today=date(2026,9,18))
        self.assertEqual(read.call_count,1)
        self.assertEqual(result['quality']['uncategorized_count'],0)
        self.assertEqual(result['baseline']['months'],0)
        self.assertTrue(result['wealth']['is_partial'])

    def test_recurring_shares_sources_without_changing_original_payment_evidence(self):
        reference_patterns=analytics.detect_recurring(self.conn)
        reference=recurring_views.overview(self.conn,reference_patterns)
        rows=analytics._rows(self.conn)
        spending=[component for row in rows if (component:=analytics._household_component(row)) is not None and component['amount']<0]
        with patch.object(analytics,'_rows',side_effect=AssertionError('Unexpected full ledger scan')), \
             patch.object(analytics,'_spending_rows',side_effect=AssertionError('Unexpected full spending scan')):
            patterns=analytics.detect_recurring(self.conn,source_rows=rows,spending_rows=spending)
            shared=recurring_views.overview(self.conn,patterns,spending_rows=spending)
        self.assertEqual(shared,reference)

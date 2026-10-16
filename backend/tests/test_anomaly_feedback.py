import json
import sqlite3
import unittest
from app.db import SCHEMA
from app import analytics


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('a','Bank','credit','Card'),('b','Bank','credit','Other card')")
        for i, amount in enumerate([-10, -10, -11, -30], 1):
            self.conn.execute("INSERT INTO transactions(id,account_id,txn_date,description,merchant,merchant_kind,amount,category,dedupe_key) VALUES(?, 'a','2026-05-10','TfL travel','TfL','merchant',?,'Transport',?)", (i,amount,str(i)))
        self.conn.execute("INSERT INTO anomaly_reviews(dedupe_key,outcome,snapshot) VALUES('1','expected',?)", (json.dumps({'kind':'possible_duplicate'}),))

    def tearDown(self):
        self.conn.close()

    def candidates(self):
        return [{**dict(r), 'kind':'possible_duplicate'} for r in self.conn.execute('SELECT t.*,a.account_type,a.bank FROM transactions t JOIN accounts a ON a.id=t.account_id')]

    def test_same_scope_near_amount_learns_but_large_change_does_not(self):
        groups = analytics.review_feedback(self.conn, self.candidates())
        self.assertEqual([r['id'] for r in groups['learned']], [2,3])
        self.assertEqual([r['id'] for r in groups['open']], [4])
        self.assertEqual(groups['learned'][0]['learning_source_id'], 1)
        self.assertIn('10%', groups['learned'][0]['learning_reason'])
        # Inferred matches do not recursively extend the £10 example's radius.
        self.conn.execute("UPDATE transactions SET amount=-12 WHERE id=4")
        self.assertEqual([r['id'] for r in analytics.review_feedback(self.conn,self.candidates())['open']], [4])

    def test_scope_date_kind_and_explicit_reopen_prevent_learning(self):
        candidates=self.candidates()
        for field, value in [('account_id','b'),('merchant','Other'),('merchant_kind','person'),('category','Shopping'),('txn_date','2025-01-01'),('kind','large_amount')]:
            changed=[{**r,field:value} if r['id']==2 else r for r in candidates]
            self.assertIn(2,[r['id'] for r in analytics.review_feedback(self.conn,changed)['open']],field)
        self.conn.execute("INSERT INTO anomaly_review_exceptions(dedupe_key) VALUES('2')")
        self.assertIn(2,[r['id'] for r in analytics.review_feedback(self.conn,candidates)['open']])

    def test_confirmed_issue_vetoes_nearby_normal_example(self):
        self.conn.execute("INSERT INTO anomaly_reviews(dedupe_key,outcome,snapshot) VALUES('3','issue',?)", (json.dumps({'kind':'possible_duplicate'}),))
        self.assertIn(2,[r['id'] for r in analytics.review_feedback(self.conn,self.candidates())['open']])
        self.assertEqual(analytics.review_feedback(self.conn,self.candidates())['learned'], [])

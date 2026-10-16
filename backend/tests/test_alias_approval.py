import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from app import main
from app.categorize import apply_categorization
from app.db import init_db


class AliasApprovalTests(unittest.TestCase):
    def setUp(self):
        # Functional API fixtures use local mode; auth/security have dedicated suites.
        self.enterContext(patch.dict('os.environ', {'FINANCE_AUTH_ENABLED': 'false', 'FINANCE_DEPLOYMENT': 'local'}))
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'alias.db'
        self.conn = init_db(self.path)
        self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('a','Bank','credit','Test card')")
        self.rule = self.conn.execute("INSERT INTO merchants(canonical_name,kind,category,source,notes) VALUES('Coffee Corner','merchant','Eating out','public_research','Keep this researched source')").lastrowid
        self.conn.execute("INSERT INTO merchant_aliases(merchant_id,pattern) VALUES(?,'Coffee Corner')", (self.rule,))
        self.conn.commit()
        self.path_patch = patch.object(main, 'DB_PATH', self.path)
        self.path_patch.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.path_patch.stop(); self.conn.close(); self.temp.cleanup()

    def add(self, source):
        cur = self.conn.execute("INSERT INTO transactions(account_id,txn_date,description,source_merchant,merchant,category,amount,raw,meta,dedupe_key) VALUES('a','2026-01-01',?,?,?,'Uncategorized',-12,?,?,?)", (source+' London', source, source, source+' raw', json.dumps({'source_merchant':source}), source))
        self.conn.commit()
        return cur.lastrowid

    def approve(self, pattern, example_id=None, rule=None):
        return self.client.post(f'/api/merchants/{rule or self.rule}/aliases', json={'pattern':pattern, 'example_id':example_id})

    def test_exact_approval_updates_rows_and_removes_suggestion_without_replacing_parent_rule(self):
        ident = self.add('Cofee Corner')
        apply_categorization(self.conn); self.conn.commit()
        suggestion = next(s for s in self.client.get('/api/merchants').json()['suggestions'] if s['example_id']==ident)
        self.assertEqual(suggestion['examples'][0]['description'], 'Cofee Corner London')
        before = tuple(self.conn.execute('SELECT description,amount,raw,meta,dedupe_key FROM transactions WHERE id=?', (ident,)).fetchone())
        original_alias = self.conn.execute('SELECT id FROM merchant_aliases WHERE merchant_id=?', (self.rule,)).fetchone()[0]
        result = self.approve(suggestion['merchant'], ident)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual((result.json()['matched_transactions'],result.json()['updated_transactions']), (1,1))
        self.assertEqual(tuple(self.conn.execute('SELECT description,amount,raw,meta,dedupe_key FROM transactions WHERE id=?', (ident,)).fetchone()), before)
        self.assertEqual(tuple(self.conn.execute('SELECT source,notes FROM merchants WHERE id=?', (self.rule,)).fetchone()), ('public_research','Keep this researched source'))
        self.assertIsNotNone(self.conn.execute('SELECT 1 FROM merchant_aliases WHERE id=?', (original_alias,)).fetchone())
        directory = self.client.get('/api/merchants').json()
        self.assertFalse(any(s['merchant']=='Cofee Corner' for s in directory['suggestions']))
        self.assertEqual(next(m for m in directory['items'] if m['id']==self.rule)['n_transactions'],1)

    def test_retry_uses_normalized_key_without_duplicate_alias_or_false_updated_count(self):
        ident = self.add('Cofee Corner')
        first = self.approve('Cofee Corner', ident).json()
        retry = self.approve('  COFEE-CORNER  ', ident)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertTrue(retry.json()['already_exists'])
        self.assertEqual(retry.json()['updated_transactions'], 0)
        self.assertEqual(retry.json()['alias_id'], first['alias_id'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM merchant_aliases WHERE merchant_id=?', (self.rule,)).fetchone()[0], 2)

    def test_exact_approval_does_not_promote_broader_aliases_or_change_unmatched_rows(self):
        for mode in ('prefix', 'contains'):
            with self.subTest(mode=mode):
                pattern = f'Local {mode} cafe'
                target = self.conn.execute("INSERT INTO merchants(canonical_name,kind,category,source,confidence) VALUES(?,'merchant','Eating out','public_research',0.82)", (f'Public {mode} cafe',)).lastrowid
                self.conn.execute('INSERT INTO merchant_aliases(merchant_id,pattern,match_mode) VALUES(?,?,?)', (target,pattern,mode))
                # A longer public match must keep winning outside the new exact
                # alias. Promoting the broad alias to user would reorder it.
                other = self.conn.execute("INSERT INTO merchants(canonical_name,kind,category,source,confidence) VALUES(?,'merchant','Shopping','public_research',0.91)", (f'Other {mode} business',)).lastrowid
                longer = pattern + ' annex'
                self.conn.execute('INSERT INTO merchant_aliases(merchant_id,pattern,match_mode) VALUES(?,?,?)', (other,longer,mode))
                exact = self.add(pattern)
                broader = self.add(pattern + ' branch')
                competing = self.add(longer)
                apply_categorization(self.conn); self.conn.commit()
                before = {ident: tuple(self.conn.execute('SELECT merchant,category,classification FROM transactions WHERE id=?',(ident,)).fetchone()) for ident in (broader,competing)}
                response = self.approve(pattern, exact, rule=target)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual((response.json()['matched_transactions'],response.json()['updated_transactions']), (1,0))
                for ident in (broader,competing):
                    self.assertEqual(tuple(self.conn.execute('SELECT merchant,category,classification FROM transactions WHERE id=?',(ident,)).fetchone()), before[ident])
                classification = json.loads(self.conn.execute('SELECT classification FROM transactions WHERE id=?',(broader,)).fetchone()[0])
                self.assertEqual((classification['source'],classification['confidence']), ('public_research',0.82))
                approved = json.loads(self.conn.execute('SELECT classification FROM transactions WHERE id=?',(exact,)).fetchone()[0])
                self.assertEqual((approved['source'],approved['confidence'],approved['match_mode']), ('user',1,'exact'))

    def test_concurrent_alias_appends_preserve_both_new_aliases_and_original(self):
        first, second = self.add('Cofee Corner'), self.add('Coffee Cornr')
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda v: self.approve(*v), [('Cofee Corner',first),('Coffee Cornr',second)]))
        self.assertEqual([r.status_code for r in responses], [200,200])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM merchant_aliases WHERE merchant_id=?', (self.rule,)).fetchone()[0], 3)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM transactions WHERE merchant='Coffee Corner'").fetchone()[0], 2)

    def test_approval_provenance_is_scoped_to_the_exact_alias_context(self):
        pattern = 'Cofee Corner'
        self.conn.execute('UPDATE merchants SET confidence=0.84 WHERE id=?',(self.rule,))
        transfer_alias = self.conn.execute("INSERT INTO merchant_aliases(merchant_id,pattern,context) VALUES(?,?,'transfer')",(self.rule,pattern)).lastrowid
        self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('bank','Bank','current','Test bank')")
        transfer = self.conn.execute("INSERT INTO transactions(account_id,txn_date,description,source_merchant,merchant,category,amount,type_code,dedupe_key) VALUES('bank','2026-01-01',?,?,?,'Uncategorized',-12,'BP','context-transfer')",('To '+pattern,pattern,pattern)).lastrowid
        card = self.add(pattern)
        apply_categorization(self.conn); self.conn.commit()
        before = tuple(self.conn.execute('SELECT merchant,category,classification FROM transactions WHERE id=?',(transfer,)).fetchone())
        response = self.client.post(f'/api/merchants/{self.rule}/aliases', json={'pattern':pattern,'context':'card','example_id':card})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual((response.json()['matched_transactions'],response.json()['updated_transactions']),(1,1))
        self.assertEqual(tuple(self.conn.execute('SELECT merchant,category,classification FROM transactions WHERE id=?',(transfer,)).fetchone()),before)
        untouched = json.loads(before[2])
        self.assertEqual((untouched['source'],untouched['confidence']),('public_research',0.84))
        self.assertIsNone(self.conn.execute('SELECT 1 FROM merchant_alias_approvals WHERE alias_id=?',(transfer_alias,)).fetchone())
        # A separate approval for the transfer context retains the card approval.
        second = self.client.post(f'/api/merchants/{self.rule}/aliases', json={'pattern':pattern,'context':'transfer','example_id':transfer})
        self.assertEqual(second.status_code,200,second.text)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM merchant_alias_approvals').fetchone()[0],2)
        for ident in (card,transfer):
            classification = json.loads(self.conn.execute('SELECT classification FROM transactions WHERE id=?',(ident,)).fetchone()[0])
            self.assertEqual((classification['source'],classification['confidence']),('user',1))

    def test_explicit_reviews_remain_and_approved_uncategorized_identity_is_not_suggested_again(self):
        ident = self.add('Cofee Corner')
        self.client.post(f'/api/transactions/{ident}/review', json={'merchant':'Personal label','category':'Uncategorized'})
        result = self.approve('Cofee Corner', ident).json()
        self.assertEqual(result['preserved_reviews'], 1)
        row = self.client.get(f'/api/transactions/{ident}/review').json()['transaction']
        self.assertEqual((row['merchant'],row['category']), ('Personal label','Uncategorized'))
        self.assertFalse(any(s['merchant']=='Cofee Corner' for s in self.client.get('/api/merchants').json()['suggestions']))

    def test_dismissal_is_persistent_and_restore_does_not_change_transactions(self):
        ident = self.add('Cofee Corner')
        apply_categorization(self.conn); self.conn.commit()
        before = tuple(self.conn.execute('SELECT * FROM transactions WHERE id=?',(ident,)).fetchone())
        response = self.client.post(f'/api/merchants/{self.rule}/suggestions/dismiss', json={'pattern':'Cofee Corner'})
        self.assertEqual(response.status_code, 200)
        reopened = init_db(self.path); reopened.close()
        directory = self.client.get('/api/merchants').json()
        self.assertFalse(any(s['suggested_merchant_id']==self.rule and s['merchant']=='Cofee Corner' for s in directory['suggestions']))
        self.assertEqual(directory['dismissed_suggestions'][0]['merchant'], 'Cofee Corner')
        self.client.post(f'/api/merchants/{self.rule}/suggestions/restore', json={'pattern':'COFEE-CORNER'})
        self.assertTrue(any(s['suggested_merchant_id']==self.rule for s in self.client.get('/api/merchants').json()['suggestions']))
        self.assertEqual(tuple(self.conn.execute('SELECT * FROM transactions WHERE id=?',(ident,)).fetchone()), before)

    def test_stale_target_example_and_conflicting_alias_fail_without_partial_writes(self):
        ident = self.add('Cofee Corner')
        count = self.conn.execute('SELECT COUNT(*) FROM merchant_aliases').fetchone()[0]
        self.assertEqual(self.approve('Cofee Corner', ident, rule=999999).status_code,404)
        self.assertEqual(self.approve('Changed label', ident).status_code,409)
        other = self.conn.execute("INSERT INTO merchants(canonical_name,kind) VALUES('Different business','merchant')").lastrowid
        self.conn.execute("INSERT INTO merchant_aliases(merchant_id,pattern) VALUES(?,'Cofee Corner')", (other,)); self.conn.commit()
        response = self.approve('Cofee Corner', ident)
        self.assertEqual(response.status_code,409)
        self.assertIn('Different business',response.json()['detail'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM merchant_aliases').fetchone()[0],count+1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM merchant_suggestion_decisions').fetchone()[0],0)

    def test_short_processor_suggestion_has_context_warning_and_never_applies_itself(self):
        target = self.conn.execute("INSERT INTO merchants(canonical_name,kind,category) VALUES('Summit Housing','merchant','Housing')").lastrowid
        self.conn.execute("INSERT INTO merchant_aliases(merchant_id,pattern) VALUES(?,'Sumup Summit Housi')", (target,)); self.conn.commit()
        ident = self.add('Sumup Summit')
        apply_categorization(self.conn); self.conn.commit()
        suggestion = next(s for s in self.client.get('/api/merchants').json()['suggestions'] if s['example_id']==ident)
        self.assertIn('do not establish', suggestion['match_warning'])
        self.assertEqual(self.conn.execute('SELECT merchant FROM transactions WHERE id=?',(ident,)).fetchone()[0], 'Sumup Summit')

    def test_removing_approved_alias_restores_source_and_reopens_suggestion(self):
        ident = self.add('Cofee Corner')
        self.approve('Cofee Corner', ident)
        rule = next(r for r in self.client.get('/api/merchants').json()['items'] if r['id']==self.rule)
        rule['aliases'] = [a for a in rule['aliases'] if a['pattern']=='Coffee Corner']
        response = self.client.put(f'/api/merchants/{self.rule}', json=rule)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.conn.execute('SELECT merchant FROM transactions WHERE id=?',(ident,)).fetchone()[0], 'Cofee Corner')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM merchant_alias_approvals').fetchone()[0],0)
        self.assertTrue(any(s['suggested_merchant_id']==self.rule for s in self.client.get('/api/merchants').json()['suggestions']))


if __name__ == '__main__':
    unittest.main()

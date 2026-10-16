"""Explicit available-credit preferences survive refreshes and fail closed on rebinding."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import bank_balance_rules as rules, lunchflow_store as banks
from app.db import connect, init_db


class BankBalanceRuleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'rules.db'
        self.conn = init_db(self.path)
        self.addCleanup(self.conn.close)
        banks.ensure_schema(self.conn)
        self.conn.execute("INSERT INTO accounts(id,bank,account_type) VALUES('card','Synthetic','credit')")
        self.conn.execute("INSERT INTO accounts(id,bank,account_type) VALUES('other','Synthetic','credit')")
        self.conn.execute("""INSERT INTO bank_connections(id,account_id,name,institution_name,currency,
            status,raw,first_seen_at,last_seen_at,balance_multiplier)
            VALUES('feed','card','Card','Synthetic','GBP','ACTIVE','{}','2026-09-01','2026-09-18',-1)""")
        self.conn.execute("""INSERT INTO bank_balance_observations(connection_id,amount,currency,fetched_at)
            VALUES('feed',7145.65,'GBP','2026-09-18T12:00:00+00:00')""")
        self.conn.commit()

    def save(self, **kwargs):
        return rules.set_rule(self.conn, 'feed', **({'balance_type':'available_credit',
            'credit_limit':8750,'currency':'GBP'} | kwargs))

    def test_rule_persists_after_reopen_and_read_does_not_write(self):
        with self.conn:
            saved = self.save()
        another = connect(self.path)
        try:
            before = another.total_changes
            self.assertEqual(rules.get_rule(another, 'feed'), saved)
            self.assertEqual(another.total_changes, before)
            self.assertEqual(saved['account_id'], 'card')
            self.assertEqual(saved['currency'], 'GBP')
            self.assertTrue(saved['confirmed_at'])
        finally:
            another.close()

    def test_invalid_limits_and_wrong_account_type_or_currency_are_rejected(self):
        for value in (None, 0, -10, True, 'NaN', 'Infinity', '8750.001'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.save(credit_limit=value)
        for currency in ('USD', 'gbp', None, ''):
            with self.subTest(currency=currency), self.assertRaises(ValueError):
                self.save(currency=currency)
        self.conn.execute("UPDATE accounts SET account_type='debit' WHERE id='card'")
        with self.assertRaises(ValueError):
            self.save()
        with self.assertRaises(LookupError):
            rules.set_rule(self.conn, 'missing', balance_type='reported')

    def test_rule_never_follows_changed_or_recreated_connection_binding(self):
        self.save()
        for assignment in ("account_id='other'", "currency='USD'", "excluded=1",
                           "first_seen_at='2026-09-18'"):
            with self.subTest(assignment=assignment):
                self.conn.execute('SAVEPOINT binding')
                self.conn.execute('UPDATE bank_connections SET ' + assignment)
                self.assertIsNone(rules.get_rule(self.conn, 'feed'))
                self.assertIsNone(rules.available_credit_rule(self.conn, 'card', 'GBP'))
                self.assertIsNone(rules.available_credit_rule(self.conn, 'other', 'GBP'))
                self.conn.execute('ROLLBACK TO binding')
                self.conn.execute('RELEASE binding')
        self.assertIsNone(rules.available_credit_rule(self.conn, 'card', 'USD'))

    def test_malformed_saved_preferences_are_ignored(self):
        saved = self.save()
        for value in ('not json', '[]', json.dumps(saved | {'confirmed_at':None}),
                      json.dumps(saved | {'credit_limit':-10}),
                      json.dumps(saved | {'provider_id':'elsewhere'})):
            self.conn.execute('UPDATE preferences SET value=? WHERE key=?', (value, rules.PREFIX+'feed'))
            self.assertIsNone(rules.get_rule(self.conn, 'feed'))

    def test_reported_is_reversible_without_modifying_observations(self):
        self.save()
        restored = rules.set_rule(self.conn, 'feed', balance_type='reported')
        self.assertEqual(rules.get_rule(self.conn, 'feed'), restored)
        self.assertIsNone(rules.available_credit_rule(self.conn, 'card', 'GBP'))
        self.assertEqual(self.conn.execute('SELECT amount FROM bank_balance_observations').fetchone()[0], 7145.65)
        self.save(credit_limit=9000)
        self.assertEqual(rules.get_rule(self.conn, 'feed')['credit_limit'], 9000)

    def test_endpoint_requires_existing_request_protection_and_returns_effective_summary(self):
        from fastapi.testclient import TestClient
        from app import main
        environment = {'FINANCE_AUTH_ENABLED':'true', 'FINANCE_DEPLOYMENT':'local',
                       'LUNCH_FLOW_SYNC_SECONDS':'0', 'SNAPTRADE_SYNC_SECONDS':'0',
                       'FINANCE_AUTH_DB_PATH':str(self.path.parent/'auth.db'),
                       'FINANCE_AUTH_KEY_PATH':str(self.path.parent/'auth.key')}
        body = {'balance_type':'available_credit','credit_limit':8750,'currency':'GBP'}
        with patch.dict(os.environ, environment), patch.object(banks, 'DB_PATH', self.path), \
                patch.object(main, 'DB_PATH', self.path), patch('app.auth.session_valid', return_value=True), \
                patch('app.lunchflow_client.read') as provider, \
                TestClient(main.app, base_url='https://testserver') as client:
            url = '/api/banking/accounts/feed/balance-rule'
            self.assertEqual(client.post(url, json=body).status_code, 403)
            self.assertEqual(client.post(url, json=body, headers={'X-Ledger-Request':'1','Origin':'https://attacker.invalid'}).status_code, 403)
            response = client.post(url, json=body, headers={'X-Ledger-Request':'1'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            account = response.json()['accounts'][0]
            self.assertEqual(account['balance']['amount'], -1604.35)
            self.assertEqual(account['balance_rule']['credit_limit'], 8750)
            self.assertEqual(account['valuation']['basis'], 'available_credit')
            for invalid in (body | {'credit_limit':True}, body | {'credit_limit':0}, body | {'unsupported':1}):
                self.assertEqual(client.post(url, json=invalid, headers={'X-Ledger-Request':'1'}).status_code, 422)
            self.assertEqual(client.post(url, json=body | {'currency':'USD'}, headers={'X-Ledger-Request':'1'}).status_code, 409)
            self.assertEqual(client.post(url.replace('/feed/', '/missing/'), json=body, headers={'X-Ledger-Request':'1'}).status_code, 404)
            response = client.post(url, json={'balance_type':'reported'}, headers={'X-Ledger-Request':'1'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['accounts'][0]['balance_rule']['balance_type'], 'reported')
            provider.assert_not_called()


if __name__ == '__main__':
    unittest.main()

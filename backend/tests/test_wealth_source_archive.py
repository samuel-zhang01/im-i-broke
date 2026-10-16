"""Investment source retirement is reversible and never deletes account evidence."""
from contextlib import closing
from copy import deepcopy
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import db, main, source_reconciliation as sources, wealth


def source(identifier, *, kind='investment', value=100):
    return {'id': identifier, 'kind': kind, 'default_included': True,
            'is_paper': False, 'exclusion_reason': None,
            'observations': [{'date': '2026-09-18', 'value': value,
                              'currency': 'GBP', 'source': 'recorded', 'reconciled': True}]}


class SourceArchiveTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.accounts = [source('connected:current'), source('connected:old', value=0),
                         source('statement:card', kind='credit', value=-100)]

    def projected(self):
        return sources.apply_archive_preferences(
            sources.apply_links(deepcopy(self.accounts), sources.list_links(self.conn)),
            sources.list_archive_preferences(self.conn))

    def archive(self, source_id='connected:old', archived=True):
        return sources.set_archived(self.conn, source_id=source_id, archived=archived,
                                    accounts=self.accounts)

    def test_reads_and_redundant_restore_do_not_create_tables(self):
        self.assertEqual(sources.list_archive_preferences(self.conn), [])
        self.assertFalse(self.archive(archived=False))
        self.assertTrue(all(not account['archived_by_user'] for account in self.projected()))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM sqlite_master').fetchone()[0], 0)

    def test_archive_restore_preserves_values_and_history_and_audits_only_changes(self):
        original = deepcopy(self.accounts)
        self.assertTrue(self.archive())
        self.assertFalse(self.archive())
        archived = self.projected()[1]
        self.assertTrue(archived['archived_by_user'])
        self.assertFalse(archived['default_included'])
        self.assertEqual(archived['observations'], original[1]['observations'])
        self.assertNotIn('archived', archived, 'User preference must not mark current value unavailable')
        self.assertEqual(self.accounts, original)
        self.assertTrue(self.archive(archived=False))
        self.assertFalse(self.archive(archived=False))
        restored = self.projected()[1]
        self.assertFalse(restored['archived_by_user'])
        self.assertTrue(restored['default_included'])
        self.assertIsNone(restored['exclusion_reason'])
        self.assertEqual([row['action'] for row in self.conn.execute(
            'SELECT action FROM wealth_source_preference_events ORDER BY id')], ['archive', 'restore'])
        self.assertFalse(sources.list_archive_preferences(self.conn)[0]['archived'])

    def test_unknown_and_noninvestment_sources_cannot_be_archived(self):
        for identifier in ('missing', 'statement:card'):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                self.archive(identifier)
        with self.assertRaises(ValueError):
            self.archive(archived='false')
        self.assertEqual(sources.list_archive_preferences(self.conn), [])
        self.assertEqual(self.conn.total_changes, 0)

    def test_archive_does_not_merge_similar_sources_or_redirect_a_linked_alias(self):
        self.accounts.append(source('statement:old'))
        sources.set_link(self.conn, source_id='statement:old', canonical_id='connected:current',
            evidence_kind='owner_confirmed', evidence_digest='a' * 64, accounts=self.accounts)
        self.archive('statement:old')
        projection = {account['id']: account for account in self.projected()}
        self.assertTrue(projection['statement:old']['archived_by_user'])
        self.assertEqual(projection['statement:old']['linked_to'], 'connected:current')
        self.assertEqual(projection['statement:old']['source_status'], 'linked')
        self.assertTrue(projection['connected:current']['default_included'])
        self.assertTrue(projection['connected:old']['default_included'])
        self.archive('statement:old', archived=False)
        self.assertFalse(self.projected()[-1]['default_included'], 'Restoring an alias must not count it twice')


class SourceArchiveRouteTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'finance.db'
        conn = db.init_db(self.path)
        conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('card','Synthetic','credit','Card')")
        conn.execute("INSERT INTO statements(account_id,source_file,period_end,closing_balance,reconciled) VALUES('card','card.pdf','2026-09-18',10,1)")
        conn.commit()
        conn.close()
        cached = {'accounts': [{'id': 'broker', 'name': 'Synthetic broker',
            'institution': 'Synthetic', 'total_value': 100, 'currency': 'GBP',
            'is_paper': False, 'holdings_synced_at': '2026-09-18', 'history': {'data': []}}],
            'connections': {'data': []}, 'cache': {}, 'retrieved_at': '2026-09-18'}
        patches = [
            patch.object(db, 'DB_PATH', self.path),
            patch.object(main, 'DB_PATH', self.path),
            patch.object(wealth.snaptrade_store, 'load_portfolio', return_value=cached),
            patch.object(wealth.snaptrade_store, 'list_account_observations', return_value=[]),
            patch.object(wealth.snaptrade_store, 'list_account_estimates', return_value=[]),
            patch.dict(os.environ, {'FINANCE_DEPLOYMENT': 'trusted-lan', 'FINANCE_AUTH_ENABLED': 'false',
                'FINANCE_COOKIE_SECURE': 'false', 'FINANCE_PUBLIC_ORIGIN': '',
                'FINANCE_ALLOWED_HOSTS': 'testserver,localhost,127.0.0.1'}),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

    def archive(self, identifier='connected:broker', archived=True, **kwargs):
        return self.client.post('/api/wealth/sources/archive',
            json={'source_id': identifier, 'archived': archived},
            headers={'X-Ledger-Request': '1', **kwargs})

    def test_archive_persists_default_exclusion_and_explicit_selection_can_inspect_then_restore(self):
        response = self.archive()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'source_id': 'connected:broker', 'archived': True, 'changed': True})
        self.assertEqual(response.headers['cache-control'], 'no-store')
        result = self.client.get('/api/wealth').json()
        self.assertNotIn('connected:broker', result['selected_account_ids'])
        self.assertEqual(result['summary']['net_worth'], -10)
        archived = next(account for account in result['accounts'] if account['id'] == 'connected:broker')
        self.assertTrue(archived['archived_by_user'])
        self.assertEqual(archived['value'], 100)
        explicit = self.client.get('/api/wealth', params={'account': 'connected:broker'}).json()
        self.assertEqual(explicit['summary']['net_worth'], 100)
        self.assertFalse(self.archive().json()['changed'])
        self.assertTrue(self.archive(archived=False).json()['changed'])
        restored = self.client.get('/api/wealth').json()
        self.assertIn('connected:broker', restored['selected_account_ids'])
        self.assertEqual(restored['summary']['net_worth'], 90)
        with closing(db.connect(self.path)) as conn, conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM statements').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM wealth_source_preference_events').fetchone()[0], 2)

    def test_validation_and_cross_site_writes_leave_preferences_untouched(self):
        for identifier, archived in [('missing', True), ('statement:card', True), ('connected:broker', 'false')]:
            with self.subTest(identifier=identifier, archived=archived):
                self.assertEqual(self.archive(identifier, archived).status_code, 422)
        self.assertEqual(self.archive(**{'Sec-Fetch-Site': 'cross-site'}).status_code, 403)
        with closing(db.connect(self.path)) as conn, conn:
            self.assertEqual(sources.list_archive_preferences(conn), [])


if __name__ == '__main__':
    unittest.main()

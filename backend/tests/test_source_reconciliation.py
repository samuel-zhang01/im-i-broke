"""Identity decisions remain explicit, reversible and independent of holdings."""
from copy import deepcopy
import sqlite3
import unittest

from app import source_reconciliation as links


def account(id, *, kind='investment', paper=False, value=100, source='statement', when='2026-01-01'):
    return {'id': id, 'name': 'Same bank · 8270', 'institution': 'Moomoo', 'kind': kind, 'is_paper': paper,
            'default_included': True, 'exclusion_reason': None,
            'observations': [{'date': when, 'value': value, 'currency': 'GBP', 'source': source,
                              'reconciled': True, 'estimated': source == 'snaptrade_estimate'}]}


class SourceLinkTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.accounts = [account('statement:old'), account('connected:live', value=110, source='recorded', when='2026-02-01')]

    def link(self, source='statement:old', target='connected:live', **kwargs):
        links.set_link(self.conn, source_id=source, canonical_id=target, accounts=self.accounts,
                       evidence_kind=kwargs.get('kind', 'verified_statement_account_id'),
                       evidence_digest=kwargs.get('digest', 'a' * 64))

    def test_same_names_or_suffixes_do_not_merge_without_evidence(self):
        result = links.apply_links(deepcopy(self.accounts), [])
        self.assertTrue(all(a['default_included'] for a in result))
        self.assertTrue(all(a['canonical_id'] == a['id'] for a in result))
        self.assertEqual(links.list_links(self.conn), [])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='wealth_source_links'").fetchone())
        for evidence in ('same_institution', 'same_suffix', 'same_holdings'):
            with self.assertRaises(ValueError):
                self.link(kind=evidence)

    def test_explicit_link_extends_history_without_changing_original_records(self):
        original = deepcopy(self.accounts)
        self.link()
        result = links.apply_links(deepcopy(self.accounts), links.list_links(self.conn))
        self.assertEqual(self.accounts, original)
        self.assertEqual(result[0]['source_status'], 'linked')
        self.assertFalse(result[0]['default_included'])
        self.assertEqual(result[0]['linked_to'], 'connected:live')
        self.assertEqual(result[1]['source_ids'], ['statement:old', 'connected:live'])
        self.assertEqual([p['value'] for p in result[1]['observations']], [100, 110])
        self.assertEqual(len(result[0]['observations']), 1)

    def test_unlink_restores_separate_sources_and_keeps_an_audit_event(self):
        self.link()
        self.assertTrue(links.remove_link(self.conn, 'statement:old'))
        self.assertFalse(links.remove_link(self.conn, 'statement:old'))
        result = links.apply_links(deepcopy(self.accounts), links.list_links(self.conn))
        self.assertTrue(all(a['default_included'] for a in result))
        self.assertEqual([len(a['observations']) for a in result], [1, 1])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM wealth_source_link_events').fetchone()[0], 2)
        self.assertEqual(links.list_links(self.conn, include_inactive=True)[0]['active'], 0)

    def test_kind_mode_cycle_and_missing_sources_are_rejected(self):
        for source, target in (('statement:old', 'statement:old'), ('missing', 'connected:live')):
            with self.assertRaises(ValueError):
                self.link(source, target)
        self.accounts.append(account('cash', kind='cash'))
        self.accounts.append(account('paper', paper=True))
        for target in ('cash', 'paper'):
            with self.assertRaises(ValueError):
                self.link(target=target)
        self.link()
        with self.assertRaises(ValueError):
            self.link('connected:live', 'statement:old')
        with self.assertRaises(ValueError):
            self.link(digest='a full account number is not an evidence digest')

    def test_recorded_value_wins_same_day_but_statement_beats_provider_estimate(self):
        self.accounts[1]['observations'][0]['date'] = '2026-01-01'
        self.link()
        result = links.apply_links(deepcopy(self.accounts), links.list_links(self.conn))
        self.assertEqual(result[1]['observations'][0]['value'], 110)
        self.accounts[1]['observations'][0]['source'] = 'snaptrade_estimate'
        result = links.apply_links(deepcopy(self.accounts), links.list_links(self.conn))
        self.assertEqual(result[1]['observations'][0]['value'], 100)


if __name__ == '__main__':
    unittest.main()

"""Matching holdings can expose a separate history without linking identities."""
from copy import deepcopy
from datetime import date
import sqlite3
import unittest
from unittest.mock import patch

from app.wealth_investments import add_history


class Rates:
    def convert(self, value, original, target, when):
        return value if original == target else None


class HistoryAlternativeTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.accounts = [
            {'id': 'statement:export', 'name': 'Imported Trading212 history',
             'kind': 'investment', 'source': 'statement', 'observations': []},
            {'id': 'connected:live', 'name': 'Connected Trading212',
             'kind': 'investment', 'source': 'snaptrade', 'value': 90000,
             'observations': [{'date': '2026-09-01', 'value': 10000,
                               'currency': 'GBP', 'source': 'snaptrade_estimate'}]},
        ]
        self.cached = {'accounts': [{'id': 'live', 'institution': 'Trading212', 'is_paper': False}]}
        self.coverage = {
            'quantity_matched_account_id': 'live', 'quantities_reconciled': True,
            'reconciled_holdings': 62, 'quantity_check_date': '2026-09-18',
            'identity_link_required': True, 'valued_days': 2,
            'start_date': '2026-09-01', 'end_date': '2026-09-02',
        }
        self.observations = [
            {'date': f'2026-09-0{day}', 'value': value, 'currency': 'GBP',
             'source': 'reconstructed', 'valuation_basis': 'daily_holdings'}
            for day, value in [(1, 88000), (2, 88500)]
        ]

    def project(self, *, accounts=None, cached=None, coverage=None, selected_ids=None):
        accounts = deepcopy(self.accounts) if accounts is None else accounts
        report = {'sources': {'statement:export': {
            'coverage': self.coverage if coverage is None else coverage,
            'observations': self.observations, 'issues': [],
        }}}
        with patch('app.investment_history.reconstruct', return_value=report), \
                patch('app.wealth_investments.market_cache.read_market_data', return_value={}):
            add_history(self.conn, accounts, self.cached if cached is None else cached,
                        Rates(), today=date(2026, 9, 18), selected_ids=selected_ids)
        return accounts

    def test_alternative_is_metadata_only_and_does_not_merge_or_create_identity(self):
        before = deepcopy(self.accounts)
        changes = self.conn.total_changes
        accounts = self.project(selected_ids=['connected:live'])
        source, live = accounts
        self.assertEqual(live['history_alternatives'], [{
            'source_id': 'statement:export', 'name': 'Imported Trading212 history',
            'start_date': '2026-09-01', 'end_date': '2026-09-02', 'valued_days': 2,
            'reconciled_holdings': 62, 'quantity_check_date': '2026-09-18',
            'identity_link_required': True,
        }])
        self.assertEqual(live['value'], 90000)
        self.assertEqual(live['observations'], before[1]['observations'])
        self.assertEqual([row['value'] for row in live['history_observations']], [10000])
        self.assertEqual([row['value'] for row in source['history_observations']], [88000, 88500])
        self.assertNotIn('history_provider_conflict', live)
        self.assertNotIn('history_reconstruction', live)
        self.assertTrue(all('canonical_id' not in account for account in accounts))
        self.assertEqual(self.conn.total_changes, changes)
        self.assertFalse(self.conn.execute("SELECT name FROM sqlite_master WHERE name='wealth_source_links'").fetchone())

    def test_alternatives_require_a_current_existing_selected_live_account(self):
        cases = [
            {'selected_ids': []},
            {'selected_ids': ['statement:export']},
            {'cached': {'accounts': []}},
            {'cached': {'accounts': [{'id': 'another'}]}},
            {'cached': {'accounts': [{'id': 'live', 'is_paper': True}]}},
            {'accounts': [deepcopy(self.accounts[0])]},
        ]
        for options in cases:
            with self.subTest(options=options):
                self.assertTrue(all('history_alternatives' not in account for account in self.project(**options)))

    def test_failed_or_unvalued_match_and_already_linked_source_offer_no_alternative(self):
        for overrides in ({'quantities_reconciled': False}, {'valued_days': 0},
                          {'quantity_matched_account_id': None}, {'identity_link_required': False}):
            with self.subTest(overrides=overrides):
                accounts = self.project(coverage={**self.coverage, **overrides})
                self.assertNotIn('history_alternatives', accounts[1])
        linked = deepcopy(self.accounts)
        linked[0]['canonical_id'] = 'connected:live'
        accounts = self.project(accounts=linked)
        self.assertNotIn('history_alternatives', accounts[1])
        self.assertIn('history_reconstruction', accounts[1])

    def test_alternatives_clear_when_selection_or_matching_evidence_changes(self):
        accounts = self.project(selected_ids=['connected:live'])
        self.assertIn('history_alternatives', accounts[1])
        self.project(accounts=accounts, selected_ids=[])
        self.assertNotIn('history_alternatives', accounts[1])


if __name__ == '__main__':
    unittest.main()

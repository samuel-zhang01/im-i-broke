"""Safe EDA aggregates and cached-only reproducibility, using synthetic records."""
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import date
import importlib.util
import io
import json
from pathlib import Path
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

from app import investment_profile


class InvestmentProfileTests(unittest.TestCase):
    def setUp(self):
        self.portfolio = {'retrieved_at': '2026-09-18T12:00:00Z', 'accounts': [{
            'id': 'private-account-id', 'name': 'Private Example Owner', 'institution': 'Private broker name',
            'connection_id': 'private-connection', 'number_suffix': 'secret-suffix', 'total_value': 987654321,
            'positions': {'status': 'ok', 'data': [{'id': 'private-position-id', 'instrument_id': 'private-instrument-id',
                'symbol': 'PRIVATE_SYMBOL', 'name': 'Private security name', 'quantity': 987654321,
                'price': 123456789, 'value': 987654321, 'raw_symbol': 'PRIVATE_SYMBOL',
                'exchange': 'XNYS', 'listing_currency': 'USD', 'currency': 'GBP', 'asset_type': 'stock'}]},
            'history': {'status': 'ok', 'data': [{'date': '2026-09-16', 'currency': 'GBP', 'total_value': 987654321},
                                               {'date': '2026-09-18', 'currency': 'GBP', 'total_value': 987654321}]},
            'activities': {'status': 'ok', 'data': [{'id': 'private-activity-id', 'description': 'Private description',
                'type': 'DIVIDEND', 'amount': 987654321, 'currency': 'GBP'}]}}]}
        self.analysis = {'selected_account_ids': ['private-account-id'], 'accounts': [], 'holdings': [{
            'account_id': 'private-account-id', 'id': 'private-position-id', 'instrument_id': 'private-instrument-id',
            'market_status': 'ready', 'market_as_of': '2026-09-18', 'value': 987654321,
            'returns': {'1d': 2}, 'sector': 'Technology', 'day_move_estimate': 123456789}]}

    def test_no_identifiers_names_descriptions_or_personal_money_leaves_profile(self):
        review = {'items': [{'code': 'cost_basis_context', 'account_ids': ['private-account-id'],
                           'detail': 'Private note', 'evidence': [{'value': 987654321}]}]}
        result = investment_profile.profile(self.portfolio, self.analysis, review=review, today=date(2026, 9, 18))
        serialized = json.dumps(result, allow_nan=False).lower()
        for secret in ('private-account', 'private-position', 'private-instrument', 'private example', 'private broker',
                       'private_symbol', 'private security', 'secret-suffix', 'private description', 'private note',
                       '987654321', '123456789'):
            self.assertNotIn(secret, serialized)
        self.assertEqual(result['positions']['securities'], 1)
        self.assertEqual(result['concentration']['largest_instrument_pct'], 100)
        self.assertEqual(result['estimated_history']['observed_gap_calendar_days']['median'], 2)
        self.assertEqual(result['activity']['types'], {'DIVIDEND': 1})

    def test_filters_and_composite_keys_preserve_multiple_account_holdings(self):
        second = deepcopy(self.portfolio['accounts'][0])
        second['id'] = 'second-private-account'
        self.portfolio['accounts'].append(second)
        result = investment_profile.profile(self.portfolio, self.analysis)
        self.assertEqual(result['accounts']['live'], 1)
        self.analysis['selected_account_ids'].append(second['id'])
        self.analysis['holdings'].append({**self.analysis['holdings'][0], 'account_id': second['id']})
        result = investment_profile.profile(self.portfolio, self.analysis)
        self.assertEqual(result['positions']['securities'], 2)
        self.assertEqual(result['positions']['duplicate_account_position_extras'], 0)
        self.assertEqual(result['concentration']['multiaccount_instruments'], 1)
        self.analysis['selected_account_ids'] = []
        self.analysis['holdings'] = []
        self.assertEqual(investment_profile.profile(self.portfolio, self.analysis)['accounts']['live'], 0)

    def test_cli_reads_only_cached_analysis_endpoints(self):
        path = Path(__file__).resolve().parents[1] / 'scripts' / 'investment_eda.py'
        spec = importlib.util.spec_from_file_location('investment_eda_cli', path)
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        responses = [{'data_profile': {'safe': 'aggregate'}}, {'days': []}]
        urls = []
        def open_url(url, **kwargs):
            urls.append(url)
            return io.BytesIO(json.dumps(responses.pop(0)).encode())
        output = io.StringIO()
        with patch.object(cli, 'urlopen', side_effect=open_url), patch('sys.argv', ['investment_eda']), redirect_stdout(output):
            cli.main()
        self.assertEqual([urlsplit(url).path for url in urls], ['/api/investments/analysis/insights', '/api/investments/analysis/calendar'])
        self.assertTrue(all(urlsplit(url).netloc == '127.0.0.1:8080' for url in urls))
        self.assertEqual(json.loads(output.getvalue())['safe'], 'aggregate')


if __name__ == '__main__':
    unittest.main()

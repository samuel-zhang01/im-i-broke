"""Historical order investigation remains a bounded provider GET-only read."""
import json
import unittest
from unittest.mock import Mock, patch

from app import snaptrade_client as client


class HistoricalOrderReadTests(unittest.TestCase):
    def test_bounded_read_preserves_execution_evidence_without_inventing_activity(self):
        rows = [{'brokerage_order_id': 'filled', 'status': 'EXECUTED', 'action': 'BUY',
                 'filled_quantity': '4', 'total_quantity': '4', 'execution_price': '25',
                 'time_placed': '2026-09-01T10:00:00Z', 'time_executed': '2026-09-03T12:00:00Z',
                 'quote_currency': {'code': 'SGD'}, 'universal_symbol': {'symbol': 'TEST.SI'},
                 'private_raw': 'SECRET'},
                {'brokerage_order_id': 'canceled', 'status': 'CANCELED', 'action': 'BUY',
                 'filled_quantity': '0', 'total_quantity': '4', 'time_placed': '2026-09-02T10:00:00Z'}]
        read = Mock(return_value=rows)
        result = client.fetch_historical_orders('account', days=30, read=read)
        read.assert_called_once_with('historical_orders', account_id='account', days=30, state='all')
        self.assertEqual(result['data'][0]['executed_at'], '2026-09-03T12:00:00Z')
        self.assertEqual(result['data'][1]['filled_quantity'], 0)
        self.assertIsNone(result['data'][1]['executed_at'])
        self.assertEqual(result['data'][0]['currency'], 'SGD')
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(client.OPERATIONS['orders'][1], 'get_user_account_recent_orders')

    def test_invalid_lookback_and_oversized_or_failed_responses_are_bounded(self):
        read = Mock()
        for days in (0, 91, True, 2.5, '30'):
            with self.assertRaises(ValueError):
                client.fetch_historical_orders('account', days=days, read=read)
        read.assert_not_called()
        with patch.object(client, 'MAX_ROWS', 2):
            result = client.fetch_historical_orders('account', read=Mock(return_value=[{}, {}, {}]))
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['data']), 2)
        result = client.fetch_historical_orders('account', read=Mock(side_effect=RuntimeError('SECRET')))
        self.assertEqual(result['status'], 'error')
        self.assertNotIn('SECRET', json.dumps(result))

    def test_transport_permits_only_same_origin_order_reads(self):
        request = Mock(return_value='response')
        guarded = client.restrict_transport(request)
        self.assertEqual(guarded('GET', 'https://api.snaptrade.com/accounts/account/orders?days=30'), 'response')
        for method, url in [('POST', 'https://api.snaptrade.com/accounts/account/orders'),
                            ('GET', 'https://other.invalid/accounts/account/orders'),
                            ('GET', 'https://api.snaptrade.com/accounts/account/orders/execute')]:
            with self.assertRaises(ValueError):
                guarded(method, url)
        self.assertEqual(request.call_count, 1)
        self.assertFalse(request.call_args.kwargs['redirect'])


if __name__ == '__main__':
    unittest.main()

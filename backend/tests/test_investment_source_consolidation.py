"""Identity reconciliation requires corroboration, uniqueness and original bytes."""
from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app import investment_source_consolidation as consolidation


class InvestmentConsolidationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE accounts(id TEXT PRIMARY KEY,bank TEXT,account_type TEXT);
            CREATE TABLE statements(id INTEGER PRIMARY KEY,account_id TEXT,meta TEXT);
            CREATE TABLE transactions(id INTEGER PRIMARY KEY,account_id TEXT,meta TEXT);
            CREATE TABLE snaptrade_cache(id INTEGER PRIMARY KEY,payload TEXT);
            INSERT INTO accounts VALUES('Stocks','Trading212','investment');
            INSERT INTO accounts VALUES('ISA','Trading212','investment');
        ''')
        self.holdings = {symbol: {'ticker': symbol, 'net_shares': quantity, 'execution_price_currency': 'GBP'}
                         for symbol, quantity in [('AAA', 11), ('BBB', 22), ('CCC', 33), ('DDD', 44), ('EEE', 55)]}
        self.rows = []
        for index in range(12):
            trade = index < 3
            self.rows.append({'Action': 'Market buy' if trade else 'Dividend (Dividend)',
                              'Time (UTC)': f'2026-09-{14 if index == 0 else 15:02d} 10:{index:02d}:00+00:00',
                              'Ticker': ['AAA', 'BBB', 'AAA'][index] if trade else 'CCC',
                              'No. of shares': '2', 'Total': '12.34' if trade else '0.50',
                              'Currency (Total)': 'GBP', 'ID': f'fill-{index}' if trade else ''})
        path = self.root / 'source.csv'
        with path.open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(self.rows[0]))
            writer.writeheader()
            writer.writerows(self.rows)
        self.sha = hashlib.sha256(path.read_bytes()).hexdigest()
        self.conn.execute('INSERT INTO statements VALUES(1,?,?)', ('Stocks', json.dumps({'source_sha256': self.sha})))
        for index, row in enumerate(self.rows, 2):
            self.conn.execute('INSERT INTO transactions VALUES(?,?,?)',
                              (index, 'Stocks', json.dumps({'source_observations': [
                                  {'source_sha256': self.sha, 'source_line': index, 'source_row': row}]})))
        activities = [{'id': str(index), 'type': 'BUY' if index < 3 else 'DIVIDEND',
                       'date': row['Time (UTC)'], 'symbol': row['Ticker'] + '.L',
                       'quantity': 2, 'amount': -1234 if index < 3 else 50, 'currency': 'GBX'}
                      for index, row in enumerate(self.rows)]
        self.payload = {'accounts': [{'id': 'live', 'institution': 'Trading212', 'is_paper': False,
                                     'holdings_synced_at': '2026-09-18T12:00:00Z',
                                     'positions': {'status': 'ok', 'data': [
                                         {'raw_symbol': key, 'quantity': row['net_shares'], 'listing_currency': 'GBP'}
                                         for key, row in self.holdings.items()]},
                                     'activities': {'status': 'ok', 'data': activities}}]}
        self.save_payload()
        patcher = patch.object(consolidation.portfolio, 'holding_positions', side_effect=self.positions)
        patcher.start()
        self.addCleanup(patcher.stop)

    def positions(self, conn, account_ids):
        return deepcopy(self.holdings) if account_ids == ['Stocks'] else {}

    def save_payload(self):
        self.conn.execute('INSERT OR REPLACE INTO snaptrade_cache VALUES(1,?)', (json.dumps(self.payload),))
        self.conn.commit()

    def manifest(self):
        return consolidation.build_manifest(self.conn, source_id='Stocks', data_root=self.root)

    def test_verified_execution_link_is_idempotent_and_preserves_raw_tables(self):
        before = {table: [tuple(row) for row in self.conn.execute('SELECT * FROM ' + table)]
                  for table in ('accounts', 'statements', 'transactions', 'snaptrade_cache')}
        manifest = self.manifest()
        self.assertEqual(manifest['matching_event_count'], 12)
        self.assertEqual(manifest['cash_verified_execution_count'], 3)
        self.assertEqual(manifest['holdings_count'], 5)
        self.assertTrue(consolidation.apply_manifest(self.conn, manifest, data_root=self.root))
        self.assertFalse(consolidation.apply_manifest(self.conn, manifest, data_root=self.root))
        self.assertEqual(self.conn.execute('SELECT evidence_kind FROM wealth_source_links').fetchone()[0], 'verified_activity_reconciliation')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM wealth_source_link_events').fetchone()[0], 1)
        self.assertEqual(before, {table: [tuple(row) for row in self.conn.execute('SELECT * FROM ' + table)] for table in before})

    def test_quantity_cash_currency_and_missing_execution_conflicts_fail_closed(self):
        initial = deepcopy(self.payload)
        for key, value in [('quantity', 2.01), ('amount', -1200), ('currency', 'USD'), ('date', '2026-09-15T11:00:00Z')]:
            with self.subTest(key=key):
                self.payload = deepcopy(initial)
                self.payload['accounts'][0]['activities']['data'][1][key] = value
                self.save_payload()
                with self.assertRaises(ValueError):
                    self.manifest()

    def test_shared_holdings_without_enough_independent_executions_are_rejected(self):
        self.payload['accounts'][0]['activities']['data'] = self.payload['accounts'][0]['activities']['data'][3:]
        self.save_payload()
        with self.assertRaisesRegex(ValueError, 'Insufficient independent'):
            self.manifest()

    def test_ambiguous_connected_or_imported_accounts_are_rejected(self):
        self.payload['accounts'].append({**deepcopy(self.payload['accounts'][0]), 'id': 'other-live'})
        self.save_payload()
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            self.manifest()
        self.payload['accounts'].pop()
        self.save_payload()
        with patch.object(consolidation.portfolio, 'holding_positions', return_value=deepcopy(self.holdings)):
            with self.assertRaisesRegex(ValueError, 'Another imported'):
                self.manifest()

    def test_changed_original_bytes_and_missing_saved_provenance_are_rejected(self):
        self.conn.execute('DELETE FROM transactions WHERE id=2')
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, 'unique saved transaction'):
            self.manifest()
        (self.root / 'source.csv').write_text('different original bytes')
        with self.assertRaisesRegex(ValueError, 'byte-identical'):
            self.manifest()

    def test_manifest_revalidation_rolls_back_stale_or_modified_proof(self):
        manifest = self.manifest()
        self.payload['new_refresh'] = True
        self.save_payload()
        with self.assertRaisesRegex(ValueError, 'changed after review'):
            consolidation.apply_manifest(self.conn, manifest, data_root=self.root)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM wealth_source_links').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM wealth_source_link_events').fetchone()[0], 0)

    def test_stale_truncated_wrong_currency_and_later_trades_are_rejected(self):
        initial = deepcopy(self.payload)
        for change in ('stale', 'truncated', 'currency', 'later'):
            self.payload = deepcopy(initial)
            account = self.payload['accounts'][0]
            if change in ('stale', 'truncated'):
                account['positions'][change] = True
            elif change == 'currency':
                account['positions']['data'][0]['listing_currency'] = 'USD'
            else:
                account['activities']['data'].append({**account['activities']['data'][0], 'id': 'later', 'date': '2026-09-17T12:00:00Z'})
            self.save_payload()
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.manifest()

    def test_duplicate_api_executions_are_not_counted_as_independent_proof(self):
        account = self.payload['accounts'][0]
        account['activities']['data'].append({**account['activities']['data'][0], 'id': 'duplicate'})
        self.save_payload()
        with self.assertRaisesRegex(ValueError, 'Duplicate API'):
            self.manifest()


if __name__ == '__main__':
    unittest.main()

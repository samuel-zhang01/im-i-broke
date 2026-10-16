"""Overlapping daily/monthly sources preserve one ledger event and both scopes."""
import copy
import hashlib
import json
from datetime import date
from pathlib import Path
import tempfile
import unittest

from app.db import init_db
from app.ingest import _store_statement
from app.parsers.base import ParsedStatement, RawTxn


class DailyMonthlyMoomooIngestTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.conn = init_db(Path(folder.name) / 'ledger.db')
        self.addCleanup(self.conn.close)

    def statement(self, start, *, extra=False):
        txns = [RawTxn(date(2026, 9, 14), 'Market buy TEST', 0, type_code='Market buy',
                       ext_id='moomoo-event:synthetic-shared-fill', raw='original execution evidence',
                       meta={'broker': 'Moomoo', 'parser': 'moomoo', 'action': 'Market buy',
                             'time': '2026-09-14 10:00:00', 'ticker': 'TEST', 'isin': 'SYNTHETIC001',
                             'shares': 1, 'native_currency': 'USD', 'native_amount': -10,
                             'cash_movements': {'USD': -10}, 'source_statement_end': '2026-09-14',
                             'source_row': 'original execution evidence'})]
        if extra:
            txns.insert(0, RawTxn(date(2026, 9, 4), 'Deposit', 0, type_code='Deposit',
                                 ext_id='moomoo-event:synthetic-earlier-deposit', raw='earlier deposit evidence',
                                 meta={'broker': 'Moomoo', 'parser': 'moomoo', 'action': 'Deposit',
                                       'time': '2026-09-04 10:00:00', 'native_currency': 'USD',
                                       'native_amount': 10, 'cash_movements': {'USD': 10},
                                       'source_statement_end': '2026-09-14'}))
        return ParsedStatement('Moomoo', 'investment', 'Moomoo-synthetic', None, '999900001234',
                               start, date(2026, 9, 14), None, None, txns, meta={
                                   'parser': 'moomoo', 'native_reconciled': True,
                                   'cash_balances': {'USD': 0}, 'opening_cash_balances': {'USD': 0 if extra else 10},
                                   'net_asset_value': 10, 'portfolio_value': 10,
                                   'positions': [{'isin': 'SYNTHETIC001', 'shares': 1, 'price': 10, 'currency': 'USD'}]})

    def test_same_end_different_periods_reuse_event_and_preserve_both_replay_scopes(self):
        daily = self.statement(date(2026, 9, 14))
        monthly = self.statement(date(2026, 9, 1), extra=True)
        daily.txns[0].meta['source_legs'] = [{'time':'2026-09-14 10:00:00','currency':'USD',
                                             'native':-10,'direction':'FROM','from_currency':'USD',
                                             'to_currency':'SGD','rate':1.2,'source_page':1}]
        monthly.txns[-1].meta['source_legs'] = [{**daily.txns[0].meta['source_legs'][0], 'source_page':5}]
        _store_statement(self.conn, daily, 'daily.pdf')
        original = dict(self.conn.execute('SELECT * FROM transactions').fetchone())
        result = _store_statement(self.conn, monthly, 'monthly.pdf')
        self.assertEqual(result['new_txns'], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM statements').fetchone()[0], 2)
        shared = dict(self.conn.execute('SELECT * FROM transactions WHERE id=?', (original['id'],)).fetchone())
        self.assertEqual(shared['raw'], original['raw'])
        self.assertEqual(shared['statement_id'], original['statement_id'])
        self.assertEqual(json.loads(shared['meta'])['source_legs'][0]['source_page'], 1)
        scopes = {(p['source_statement_start'], p['source_statement_end'])
                  for p in json.loads(shared['meta'])['source_observations']}
        self.assertEqual(scopes, {('2026-09-14', '2026-09-14'), ('2026-09-01', '2026-09-14')})
        first_pass = [tuple(row) for row in self.conn.execute('SELECT * FROM transactions ORDER BY id')]
        for statement, name in ((monthly, 'monthly.pdf'), (daily, 'daily.pdf')):
            self.assertEqual(_store_statement(self.conn, statement, name)['new_txns'], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 2)
        self.assertEqual([tuple(row) for row in self.conn.execute('SELECT * FROM transactions ORDER BY id')], first_pass)
        deposit = json.loads(self.conn.execute("SELECT meta FROM transactions WHERE type_code='Deposit'").fetchone()[0])
        self.assertEqual(deposit['source_statement_start'], '2026-09-01')
        changed = copy.deepcopy(monthly)
        changed.txns.pop(0)
        with self.assertRaisesRegex(ValueError, 'changed events'):
            _store_statement(self.conn, changed, 'monthly-revised.pdf')
        changed_leg = copy.deepcopy(monthly)
        changed_leg.txns[-1].meta['source_legs'][0]['rate'] = 1.3
        with self.assertRaisesRegex(ValueError, 'changed cash leg evidence'):
            _store_statement(self.conn, changed_leg, 'monthly-conflicting-leg.pdf')

    def test_legacy_monthly_scope_is_recovered_before_later_daily_evidence(self):
        monthly = self.statement(date(2026, 9, 1), extra=True)
        daily = self.statement(date(2026, 9, 14))
        _store_statement(self.conn, monthly, 'monthly.pdf')
        shared = dict(self.conn.execute("SELECT * FROM transactions WHERE type_code='Market buy'").fetchone())
        meta = json.loads(shared['meta'])
        for observation in meta['source_observations']:
            observation.pop('source_statement_start')
            observation.pop('source_statement_end')
        self.conn.execute('UPDATE transactions SET meta=? WHERE id=?', (json.dumps(meta), shared['id']))
        self.assertEqual(_store_statement(self.conn, daily, 'daily.pdf')['new_txns'], 0)
        primary = json.loads(self.conn.execute('SELECT meta FROM transactions WHERE id=?', (shared['id'],)).fetchone()[0])
        self.assertEqual(primary['source_statement_start'], '2026-09-01')
        self.assertEqual(primary['source_statement_end'], '2026-09-14')
        scopes = {(p['source_statement_start'], p['source_statement_end']) for p in json.loads(
            self.conn.execute('SELECT meta FROM transactions WHERE id=?', (shared['id'],)).fetchone()[0])['source_observations']}
        self.assertEqual(scopes, {('2026-09-01', '2026-09-14'), ('2026-09-14', '2026-09-14')})
        self.assertEqual(_store_statement(self.conn, monthly, 'monthly.pdf')['new_txns'], 0)
        self.assertEqual(_store_statement(self.conn, daily, 'daily.pdf')['new_txns'], 0)

    def test_equivalent_reexport_preserves_each_document_hash_without_duplicate_events(self):
        with tempfile.TemporaryDirectory() as folder:
            first = Path(folder) / 'original' / 'daily.pdf'
            later = Path(folder) / 'reexport' / 'daily.pdf'
            for path, payload in ((first, b'synthetic original bytes'), (later, b'synthetic reexport bytes')):
                path.parent.mkdir()
                path.write_bytes(payload)
            s = self.statement(date(2026, 9, 14))
            _store_statement(self.conn, s, str(first))
            original = dict(self.conn.execute('SELECT * FROM statements').fetchone())
            self.assertEqual(_store_statement(self.conn, s, str(later))['new_txns'], 0)
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM statements').fetchone()[0], 2)
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 1)
            self.assertEqual(dict(self.conn.execute('SELECT * FROM statements WHERE id=?', (original['id'],)).fetchone()), original)
            for row in self.conn.execute('SELECT source_file,meta FROM statements'):
                self.assertEqual(json.loads(row['meta'])['source_sha256'], hashlib.sha256(Path(row['source_file']).read_bytes()).hexdigest())
            self.assertEqual(_store_statement(self.conn, s, str(later))['new_txns'], 0)

    def test_changed_reported_opening_nav_is_not_silently_replaced(self):
        s = self.statement(date(2026, 9, 14))
        s.meta['opening_net_asset_value'] = 20
        _store_statement(self.conn, s, 'daily.pdf')
        before = dict(self.conn.execute('SELECT * FROM statements').fetchone())
        changed = copy.deepcopy(s)
        changed.meta['opening_net_asset_value'] = 21
        with self.assertRaisesRegex(ValueError, 'changed its cash or position snapshot'):
            _store_statement(self.conn, changed, 'changed-daily.pdf')
        self.assertEqual(dict(self.conn.execute('SELECT * FROM statements').fetchone()), before)


if __name__ == '__main__':
    unittest.main()

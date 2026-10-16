import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app import analytics
from app.broker_snapshots import broker_snapshot_status
from app.db import init_db
from app.portfolio import holding_positions


class BrokerSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.conn=init_db(Path(self.tmp.name)/'qa.db')
        self.conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('moo','Moomoo','investment','Moomoo test')")

    def tearDown(self):
        self.conn.close();self.tmp.cleanup()

    def statement(self,end,previous,opening_shares,closing_shares,opening_cash,closing_cash):
        positions=[{'ticker':'AAPL','isin':'US0378331005','shares':closing_shares,'currency':'USD','price':110,'market_value':closing_shares*110}] if closing_shares else []
        meta={'parser':'moomoo','snapshot_date':end,'previous_snapshot_date':previous,'native_reconciled':True,
              'positions':positions,'position_changes':[{'isin':'US0378331005','starting_shares':opening_shares}],
              'cash_balances':{'USD':closing_cash},'opening_cash_balances':{'USD':opening_cash},'base_currency':'SGD'}
        return self.conn.execute('INSERT INTO statements(account_id,source_file,period_end,meta) VALUES(?,?,?,?)',
                                 ('moo',end+'.pdf',end,json.dumps(meta))).lastrowid

    def event(self,stmt,period,shares,cash):
        meta={'source_statement_end':period,'shares':shares,'isin':'US0378331005','cash_movements':{'USD':cash}}
        self.conn.execute('INSERT INTO transactions(account_id,statement_id,txn_date,description,amount,meta,dedupe_key) VALUES(?,?,?,?,?,?,?)',
                          ('moo',stmt,period,'Synthetic event',0,json.dumps(meta),str(stmt)+'-'+str(shares)+'-'+str(cash)))

    def status(self):return broker_snapshot_status(self.conn)['moo']

    def test_complete_zero_opening_contiguous_history_needs_no_fallback(self):
        a=self.statement('2026-07-31','2026-06-30',0,2,0,799);self.event(a,'2026-07-31',2,799)
        b=self.statement('2026-08-31','2026-07-31',2,3,799,698);self.event(b,'2026-08-31',1,-101)
        status=self.status()
        self.assertTrue(status['history_complete']);self.assertFalse(status['use_snapshot_fallback'])
        self.assertEqual(status['fallback_positions'],[]);self.assertEqual(status['warnings'],[])
        self.assertEqual(status['cash_balances'],{'USD':698})

    def test_only_later_quiet_month_preserves_snapshot_wealth_unknown_cost(self):
        self.statement('2026-08-31','2026-07-31',25,25,500,500)
        status=self.status()
        self.assertFalse(status['history_complete']);self.assertTrue(status['use_snapshot_fallback'])
        self.assertEqual(status['fallback_positions'][0]['net_shares'],25)
        self.assertIsNone(status['fallback_positions'][0]['cost_basis_gbp'])
        self.assertIsNone(status['fallback_positions'][0]['realised_gbp'])
        self.assertEqual(status['fallback_positions'][0]['as_of'],'2026-08-31')
        self.assertEqual(status['cash_balances'],{'USD':500})

    def test_missing_middle_month_flags_gap_even_when_final_quantities_match(self):
        a=self.statement('2026-06-30','2026-05-31',0,2,0,799);self.event(a,'2026-06-30',2,799)
        self.statement('2026-08-31','2026-07-31',2,2,799,799)
        status=self.status();self.assertFalse(status['history_complete'])
        self.assertTrue(status['share_history_matches_snapshot'])
        self.assertTrue(any('gap' in warning for warning in status['warnings']))

    def test_deleted_stored_trade_detected_despite_reconciled_statement_flag(self):
        self.statement('2026-07-31','2026-06-30',0,2,0,799)
        status=self.status();self.assertFalse(status['share_history_matches_snapshot'])
        self.assertFalse(status['cash_history_matches_snapshot'])
        self.assertEqual(status['fallback_positions'][0]['net_shares'],2)

    def test_closed_snapshot_empty_is_authoritative_with_missing_opening_history(self):
        a=self.statement('2026-08-31','2026-07-31',2,0,100,320);self.event(a,'2026-08-31',-2,220)
        status=self.status();self.assertTrue(status['use_snapshot_fallback'])
        self.assertEqual(status['fallback_positions'],[])
        self.assertEqual(status['cash_balances'],{'USD':320})

    def test_reverse_import_order_and_account_scope(self):
        b=self.statement('2026-08-31','2026-07-31',2,3,799,698);self.event(b,'2026-08-31',1,-101)
        a=self.statement('2026-07-31','2026-06-30',0,2,0,799);self.event(a,'2026-07-31',2,799)
        self.assertTrue(self.status()['history_complete'])
        self.assertEqual(broker_snapshot_status(self.conn,[]),{})
        self.assertEqual(broker_snapshot_status(self.conn,['other']),{})
        self.assertEqual(set(broker_snapshot_status(self.conn,['moo'])),{'moo'})

    def test_later_only_import_values_recorded_units_and_cash_without_invented_cost(self):
        self.statement('2026-08-31','2026-07-31',25,25,500,500)
        now = datetime.now(timezone.utc)
        self.conn.execute('INSERT INTO prices(isin,symbol,currency,price_native,price_gbp,identity_verified,quote_asof,fetched_at) VALUES(?,?,?,?,?,?,?,?)',
                          ('US0378331005','AAPL','USD',110,80,1,now.isoformat(),now.isoformat()))
        self.conn.execute('INSERT INTO fx_quotes(currency,rate_to_gbp,asof) VALUES(?,?,?)', ('USD',.75,now.isoformat()))
        with patch('app.prices.batch_quote') as quotes:
            value = analytics.valuation(self.conn,now=now,account_ids=['moo'])
            quotes.assert_not_called()
        holding = value['holdings'][0]
        self.assertEqual((holding['shares'],holding['market_value']),(25,2000))
        for key in ('cost_basis','estimated_cost_basis','native_cost_basis','unrealised','estimated_unrealised','realised'):
            self.assertIsNone(holding[key],key)
        self.assertFalse(holding['history_complete'])
        self.assertEqual(holding['snapshot_sources'][0]['date'],'2026-08-31')
        self.assertEqual(value['totals']['foreign_cash'][0]['amount'],500)
        self.assertEqual(value['totals']['foreign_cash_value'],375)
        bundle=analytics.investments_bundle(self.conn,['moo'])
        self.assertEqual(bundle['native_currency_totals'][0]['cash_balance'],500)
        self.assertFalse(bundle['accounts'][0]['history_complete'])
        self.assertEqual(bundle['stats']['n_trades'],0)
        self.assertEqual(holding_positions(self.conn,[]),{})
        self.assertEqual(analytics._foreign_cash(self.conn,account_ids=[]),[])

    def test_closed_snapshot_removes_residual_units_without_fabricated_sale(self):
        a=self.statement('2026-06-30','2026-05-31',0,2,0,799);self.event(a,'2026-06-30',2,799)
        self.statement('2026-08-31','2026-07-31',0,0,1019,1019)
        p=holding_positions(self.conn,['moo'])['US0378331005']
        self.assertEqual(p['net_shares'],0)
        self.assertEqual(p['trades'],1)
        self.assertIsNone(p['realised'])
        self.assertFalse(p['history_complete'])
        self.assertEqual(analytics._foreign_cash(self.conn,account_ids=['moo'])[0]['amount'],1019)


if __name__=='__main__':unittest.main()

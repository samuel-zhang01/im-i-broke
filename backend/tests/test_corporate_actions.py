"""Corporate actions preserve execution units, cash and chronological book cost."""
import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import analytics
from app.corporate_actions import KNOWN_SPLITS, record_split, sync_known_actions
from app.db import SCHEMA, init_db
from app.ingest import ingest_file


class CorporateActionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT INTO accounts(id,bank,account_type) VALUES('invest','Test','investment')")
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def trade(self, day, shares, amount, account="invest", isin="TEST", **meta):
        self.conn.execute("INSERT INTO transactions(account_id,txn_date,merchant,type_code,amount,meta) VALUES(?,?,?,?,?,?)",
            (account, day, "TEST", "Market buy" if shares > 0 else "Market sell", amount,
             json.dumps({"isin": isin, "shares": shares, **meta})))

    def split(self, account="invest", **overrides):
        fields = {**KNOWN_SPLITS[0], "isin": "TEST", **overrides}
        return record_split(self.conn, account, **fields)

    def position(self):
        return analytics._holding_positions(self.conn)["TEST"]

    def test_split_preserves_book_cost_and_divides_unit_cost(self):
        self.trade("2024-06-06", 8.25, -825)
        self.split()
        position = self.position()
        self.assertEqual((position["net_shares"], position["cost_basis"], position["avg_cost"]), (82.5, 825, 10))
        self.assertEqual(self.conn.execute("SELECT SUM(amount) FROM transactions").fetchone()[0], -825)
        self.assertEqual(position["corporate_actions"][0]["before_shares"], 8.25)

    def test_split_before_ex_date_sale_and_buy_even_when_inserted_out_of_order(self):
        self.trade("2024-06-10", -50, 750, time="2024-06-10 10:00:00")
        self.trade("2024-06-10", 2, -40, time="2024-06-10 12:00:00")
        self.trade("2024-06-07", 10, -1000, time="2024-06-07 19:00:00")
        self.split()
        position = self.position()
        self.assertEqual((position["net_shares"], position["cost_basis"], position["realised"]), (52, 540, 250))
        self.assertTrue(position["position_complete"])

    def test_action_only_changes_its_own_account(self):
        self.conn.execute("INSERT INTO accounts(id,bank,account_type) VALUES('other','Test','investment')")
        self.trade("2024-06-06", 10, -1000)
        self.trade("2024-06-06", 3, -600, account="other")
        self.split()
        self.trade("2024-06-10", -100, 1500)
        self.assertEqual((self.position()["net_shares"], self.position()["cost_basis"]), (3, 600))

    def test_unknown_gbp_cost_stays_unknown_through_split(self):
        self.trade("2024-06-06", 10, 0, native_currency="USD", cost_basis_gbp=None)
        self.split()
        self.assertEqual(self.position()["net_shares"], 100)
        self.assertIsNone(self.position()["cost_basis"])
        self.assertIsNone(self.position()["avg_cost"])

    def test_missing_lots_are_not_cured_by_split(self):
        self.trade("2024-06-06", -2, 200)
        self.split()
        self.trade("2024-06-11", 30, -300)
        self.assertFalse(self.position()["position_complete"])

    def test_reverse_split_keeps_fractional_units_and_cost(self):
        self.trade("2024-06-06", 15, -75)
        self.split(ratio_new=1, ratio_old=10)
        self.assertEqual((self.position()["net_shares"], self.position()["cost_basis"]), (1.5, 75))

    def test_buying_after_split_is_not_adjusted(self):
        self.trade("2024-06-10", 3, -30)
        self.split()
        self.assertEqual(self.position()["net_shares"], 3)
        self.assertEqual(self.position()["corporate_actions"], [])

    def test_replay_and_repeated_registration_do_not_mutate_trades(self):
        self.trade("2024-06-06", 10, -1000)
        before = [tuple(row) for row in self.conn.execute("SELECT * FROM transactions")]
        action_id = self.split()
        self.assertEqual(action_id, self.split(ratio_new=20, ratio_old=2))
        for _ in range(3):
            self.assertEqual(self.position()["net_shares"], 100)
        self.assertEqual(before, [tuple(row) for row in self.conn.execute("SELECT * FROM transactions")])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM corporate_actions").fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, "Conflicting corporate action"):
            self.split(ratio_new=2)

    def write_csv(self, rows):
        path = Path(self.temp.name) / "trades.csv"
        with path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Action", "Time", "ISIN", "Ticker", "No. of shares", "Total", "Currency (Total)", "ID"])
            writer.writerows(rows)
        return str(path)

    def test_repeated_import_seeds_one_action_and_preserves_source_units(self):
        path = self.write_csv([
            ["Market buy", "2024-06-06 12:00:00", "US67066G1040", "NVDA", "8.25", "825", "GBP", "buy1"],
            ["Market sell", "2024-06-10 12:00:00", "US67066G1040", "NVDA", "50", "750", "GBP", "sell1"],
        ])
        self.assertEqual(ingest_file(path, conn=self.conn)["new_txns"], 2)
        self.assertEqual(ingest_file(path, conn=self.conn)["new_txns"], 0)
        self.assertEqual(sync_known_actions(self.conn), 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM corporate_actions").fetchone()[0], 1)
        position = analytics._holding_positions(self.conn)["US67066G1040"]
        self.assertEqual((position["net_shares"], position["cost_basis"]), (32.5, 325))
        self.assertTrue(position["position_complete"])

    def test_unreviewed_action_export_is_rejected_atomically(self):
        path = self.write_csv([
            ["Market buy", "2024-06-06 12:00:00", "US67066G1040", "NVDA", "10", "1000", "GBP", "buy1"],
            ["Stock split", "2024-06-10 00:00:00", "US67066G1040", "NVDA", "90", "0", "GBP", "split1"],
        ])
        with self.assertRaisesRegex(ValueError, "corporate-action rows require review"):
            ingest_file(path, conn=self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 0)

    def test_reexport_adjusting_old_trade_units_is_rejected_and_keeps_history(self):
        original = ["Market buy", "2024-06-06 12:00:00", "US67066G1040", "NVDA", "10", "1000", "GBP", "buy1"]
        ingest_file(self.write_csv([original]), conn=self.conn)
        adjusted = [*original]
        adjusted[4] = "100"
        with self.assertRaisesRegex(ValueError, "changed security or share quantity"):
            ingest_file(self.write_csv([adjusted]), conn=self.conn)
        position = analytics._holding_positions(self.conn)["US67066G1040"]
        self.assertEqual(position["net_shares"], 100)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 1)

    def test_additive_migration_keeps_legacy_rows_and_backfills_actions_once(self):
        path = Path(self.temp.name) / "legacy.db"
        conn = sqlite3.connect(path)
        conn.executescript(SCHEMA)
        conn.execute("ALTER TABLE statements DROP COLUMN meta")
        conn.execute("INSERT INTO accounts(id,bank,account_type) VALUES('invest','Test','investment')")
        conn.execute("INSERT INTO transactions(id,account_id,txn_date,amount,meta) VALUES(37,'invest','2024-06-06',-100,?)",
                     (json.dumps({"isin": "US67066G1040", "shares": 1}),))
        conn.execute("DROP TABLE corporate_actions")
        conn.commit()
        conn.close()
        for _ in range(2):
            migrated = init_db(path)
            self.assertEqual(tuple(migrated.execute("SELECT id,amount FROM transactions").fetchone()), (37, -100))
            self.assertEqual(migrated.execute("SELECT COUNT(*) FROM corporate_actions").fetchone()[0], 1)
            self.assertIn("meta", [row[1] for row in migrated.execute("PRAGMA table_info(statements)")])
            migrated.close()


if __name__ == "__main__":
    unittest.main()

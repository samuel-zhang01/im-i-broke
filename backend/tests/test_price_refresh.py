"""Provider refresh outcomes remain visible without discarding cached value."""
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import analytics
from app.db import SCHEMA


class PriceRefreshTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT INTO accounts(id,bank,account_type,display_name)"
            " VALUES('invest','Trading212','investment','Investments')")
        self.now = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
        self.network = patch('app.prices._get_json', side_effect=AssertionError('Tests must not use the network'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def tearDown(self):
        self.conn.close()

    def holding(self, isin="TEST", price=20, age=timedelta(minutes=5)):
        self.conn.execute(
            "INSERT INTO transactions(account_id,txn_date,merchant,description,amount,meta)"
            " VALUES('invest','2026-01-01',?,'Market buy',-100,?)",
            (isin, json.dumps({"isin": isin, "shares": 10, "native_currency": "GBP"})))
        if price is not None:
            self.conn.execute(
                "INSERT INTO prices(isin,symbol,currency,price_native,price_gbp,updated,quote_asof,identity_verified)"
                " VALUES(?,?,'GBP',?,?,?,?,1)",
                (isin, isin + ".L", price, price, (self.now - age).isoformat(), (self.now - age).isoformat()))
        self.conn.commit()

    def verified_quote(self, isin='TEST', price=25, age=timedelta()):
        return {'isin': isin, 'symbol': isin + '.L', 'currency': 'GBP',
                'price_native': price, 'price_gbp': price, 'identity_verified': 1,
                'quote_asof': (self.now - age).isoformat(), 'fetched_at': self.now.isoformat(),
                'fx_rate_to_gbp': 1, 'provider': 'Test provider'}

    def test_manual_provider_failure_preserves_fresh_cache_and_reports_failure(self):
        self.holding()
        previous = dict(self.conn.execute("SELECT * FROM prices").fetchone())
        # Exercise batch_quote's exception isolation too: provider errors become
        # a failed refresh result, while the valuation request still succeeds.
        with patch("app.prices.quote", side_effect=OSError("provider unavailable")):
            result = analytics.valuation(self.conn, refresh=True, now=self.now)
        outcome = result["refresh"]
        self.assertEqual((outcome["status"], outcome["requested"]), ("failed", True))
        self.assertEqual((outcome["attempted"], outcome["updated"], outcome["failed"], outcome["retained"]), (1, 0, 1, 1))
        self.assertEqual(outcome["failed_isins"], ["TEST"])
        self.assertEqual(outcome["attempted_at"], self.now.isoformat())
        self.assertEqual(dict(self.conn.execute("SELECT * FROM prices").fetchone()), previous)
        self.assertEqual(result["holdings"][0]["market_value"], 200)
        self.assertEqual(result["totals"]["n_stale"], 0)

    def test_partial_refresh_updates_success_and_preserves_failed_quote_timestamp(self):
        self.holding("ONE")
        self.holding("TWO", price=30)
        old_time = (self.now - timedelta(minutes=5)).isoformat()

        def quote(isin, symbol, **hints):
            if isin == "TWO":
                return None
            self.assertEqual(hints, {'ticker': 'ONE', 'price_currency': 'GBP'})
            return self.verified_quote(isin)

        with patch("app.prices.quote", side_effect=quote):
            result = analytics.valuation(self.conn, refresh=True, now=self.now)
        outcome = result["refresh"]
        self.assertEqual((outcome["status"], outcome["attempted"], outcome["updated"], outcome["failed"], outcome["retained"]), ("partial", 2, 1, 1, 1))
        self.assertEqual(outcome["failed_isins"], ["TWO"])
        quotes = {r["isin"]: dict(r) for r in self.conn.execute("SELECT * FROM prices")}
        self.assertEqual((quotes["ONE"]["price_gbp"], quotes["ONE"]["updated"]), (25, self.now.isoformat()))
        self.assertEqual((quotes["TWO"]["price_gbp"], quotes["TWO"]["updated"]), (30, old_time))
        self.assertEqual(result["totals"]["market_value"], 550)
        self.assertEqual(result["totals"]["asof"], old_time)

    def test_successful_refresh_reports_success(self):
        self.holding()
        new_quote = self.verified_quote()
        with patch("app.prices.quote", return_value=new_quote):
            result = analytics.valuation(self.conn, refresh=True, now=self.now)
        outcome = result["refresh"]
        self.assertEqual((outcome["status"], outcome["updated"], outcome["failed"], outcome["retained"]), ("success", 1, 0, 0))
        self.assertEqual(result["holdings"][0]["market_value"], 250)

    def test_no_refresh_needed_does_not_call_provider(self):
        self.holding()
        with patch("app.prices.batch_quote") as batch:
            result = analytics.valuation(self.conn, now=self.now)
        batch.assert_not_called()
        self.assertEqual(result["refresh"], {
            "requested": False, "status": "not_needed", "attempted": 0,
            "updated": 0, "failed": 0, "retained": 0,
            "failed_isins": [], "attempted_at": None,
        })

    def test_automatic_missing_quote_failure_remains_unpriced(self):
        self.holding(price=None)
        with patch("app.prices.quote", return_value=None):
            result = analytics.valuation(self.conn, now=self.now)
        outcome = result["refresh"]
        self.assertEqual((outcome["status"], outcome["requested"], outcome["failed"], outcome["retained"]), ("failed", False, 1, 0))
        self.assertEqual(result["totals"]["n_unpriced"], 1)
        self.assertIsNone(result["holdings"][0]["market_value"])

    def test_invalid_quote_is_failed_refresh_and_does_not_replace_cache(self):
        self.holding()
        with patch("app.prices.batch_quote", return_value={"TEST": {"price_gbp": float("nan")}}):
            result = analytics.valuation(self.conn, refresh=True, now=self.now)
        self.assertEqual(result["refresh"]["status"], "failed")
        self.assertEqual(result["refresh"]["updated"], 0)
        self.assertEqual(result["holdings"][0]["market_value"], 200)

    def test_refetch_of_old_exchange_quote_does_not_make_it_fresh(self):
        self.holding()
        new_quote = self.verified_quote(age=timedelta(days=3))
        with patch('app.prices.quote', return_value=new_quote):
            result = analytics.valuation(self.conn, refresh=True, now=self.now)
        self.assertEqual(result['refresh']['updated'], 1)
        self.assertEqual(result['totals']['n_stale'], 1)
        self.assertEqual(result['totals']['asof'], new_quote['quote_asof'])
        self.assertEqual(result['holdings'][0]['fetched_at'], self.now.isoformat())

    def test_legacy_unverified_cache_requires_identity_resolution(self):
        self.holding()
        self.conn.execute('UPDATE prices SET identity_verified=0,quote_asof=NULL')
        with patch('app.prices.quote', return_value=None):
            result = analytics.valuation(self.conn, now=self.now)
        self.assertEqual(result['refresh']['attempted'], 1)
        self.assertEqual(result['refresh']['retained'], 0)
        self.assertEqual(result['totals']['n_unpriced'], 1)
        self.assertIsNone(result['holdings'][0]['market_value'])


if __name__ == "__main__":
    unittest.main()

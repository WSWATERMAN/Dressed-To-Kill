"""Estimated mark-to-market valuation tests for mock and demo market data."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.portfolio import PaperPortfolio
from app.provider import DEMO_BASE, KalshiDemoProvider, MockProvider
import app.server as server


class PortfolioValuationTests(unittest.TestCase):
    def test_open_yes_and_no_positions_are_marked_separately_from_realized_pnl(self):
        with tempfile.TemporaryDirectory() as folder:
            portfolio = PaperPortfolio(Path(folder) / "portfolio.sqlite3")
            portfolio.add_order({"ticker":"YES-1", "title":"Yes position", "side":"yes",
                                 "quantity":4, "price":.25, "contract_cost":1.0, "fee":.04,
                                 "max_loss":1.04})
            portfolio.add_order({"ticker":"NO-1", "title":"No position", "side":"no",
                                 "quantity":2, "price":.4, "contract_cost":.8,
                                 "max_loss":.8})
            portfolio.add_order({"ticker":"DONE-1", "title":"Settled position", "side":"yes",
                                 "quantity":2, "price":.25, "max_loss":.5})
            portfolio.update_market("DONE-1", "finalized", "yes")

            snapshot = portfolio.snapshot(current_quotes={
                "YES-1": {"yes_bid": .40, "no_bid": .59},
                "NO-1": {"yes_bid": .44, "no_bid": .55},
            })
            orders = {order["ticker"]: order for order in snapshot["orders"]}

            self.assertEqual((orders["YES-1"]["estimated_mark_price"],
                              orders["YES-1"]["estimated_current_value"],
                              orders["YES-1"]["estimated_unrealized_pnl"]), (.4, 1.6, .56))
            self.assertEqual((orders["NO-1"]["estimated_mark_price"],
                              orders["NO-1"]["estimated_current_value"],
                              orders["NO-1"]["estimated_unrealized_pnl"]), (.55, 1.1, .3))
            self.assertEqual(snapshot["estimated_open_position_value"], 2.7)
            self.assertEqual(snapshot["estimated_unrealized_pnl"], .86)
            self.assertEqual(snapshot["realized_pnl"], 1.5)
            self.assertEqual(snapshot["estimated_total_value"], 1002.36)
            self.assertTrue(snapshot["valuation_complete"])
            self.assertEqual(orders["DONE-1"]["valuation_status"], "settled")
            self.assertIsNone(orders["DONE-1"]["estimated_current_value"])

    def test_missing_quote_keeps_portfolio_accessible_but_aggregate_estimates_incomplete(self):
        with tempfile.TemporaryDirectory() as folder:
            portfolio = PaperPortfolio(Path(folder) / "portfolio.sqlite3")
            portfolio.add_order({"ticker":"YES-1", "title":"Quoted position", "side":"yes",
                                 "quantity":2, "price":.4, "max_loss":.8})
            portfolio.add_order({"ticker":"NO-1", "title":"Unquoted position", "side":"no",
                                 "quantity":1, "price":.3, "max_loss":.3})

            snapshot = portfolio.snapshot(current_quotes={"YES-1": {"yes_bid": .5}})
            orders = {order["ticker"]: order for order in snapshot["orders"]}
            self.assertEqual(orders["YES-1"]["estimated_current_value"], 1.0)
            self.assertEqual(orders["YES-1"]["estimated_unrealized_pnl"], .2)
            self.assertEqual(orders["NO-1"]["valuation_status"], "unavailable")
            self.assertIsNone(snapshot["estimated_open_position_value"])
            self.assertIsNone(snapshot["estimated_unrealized_pnl"])
            self.assertIsNone(snapshot["estimated_total_value"])
            self.assertFalse(snapshot["valuation_complete"])
            self.assertEqual(snapshot["valuation_missing_count"], 1)

    def test_no_open_positions_total_value_equals_cash_and_preserves_realized_pnl(self):
        with tempfile.TemporaryDirectory() as folder:
            portfolio = PaperPortfolio(Path(folder) / "portfolio.sqlite3")
            portfolio.add_order({"ticker":"DONE-1", "title":"Settled position", "side":"no",
                                 "quantity":1, "price":.5, "max_loss":.5})
            portfolio.update_market("DONE-1", "finalized", "no")
            snapshot = portfolio.snapshot()
            self.assertEqual(snapshot["estimated_open_position_value"], 0.0)
            self.assertEqual(snapshot["estimated_unrealized_pnl"], 0.0)
            self.assertEqual(snapshot["estimated_total_value"], snapshot["cash"])
            self.assertEqual(snapshot["realized_pnl"], .5)
            self.assertTrue(snapshot["valuation_complete"])

    def test_mock_provider_portfolio_snapshot_includes_quotes(self):
        with tempfile.TemporaryDirectory() as folder:
            portfolio = PaperPortfolio(Path(folder) / "portfolio.sqlite3")
            market = MockProvider().list_markets()[0]
            portfolio.add_order({"ticker":market.ticker, "title":market.title, "side":"yes",
                                 "quantity":1, "price":.2, "max_loss":.2})
            with patch.object(server, "STORE", portfolio), patch.object(server, "PROVIDER", MockProvider()):
                snapshot = server.portfolio_snapshot()
            self.assertEqual(snapshot["orders"][0]["estimated_mark_price"], market.yes_bid)
            self.assertTrue(snapshot["valuation_complete"])

    def test_demo_market_response_exposes_side_specific_bids(self):
        provider = KalshiDemoProvider(DEMO_BASE)
        response_body = json.dumps({"markets": [{
            "ticker":"KXTEST-1", "status":"active", "result":"",
            "yes_bid_dollars":"0.4100", "no_bid_dollars":"0.5700",
        }]}).encode()
        response = type("Response", (), {
            "__enter__": lambda self:self,
            "__exit__": lambda *args:None,
            "read": lambda self,n:response_body,
        })()
        with patch("app.provider.urlopen", return_value=response):
            market = provider.get_market("KXTEST-1")
        self.assertEqual((market["yes_bid"], market["no_bid"]), (.41, .57))


if __name__ == "__main__":
    unittest.main()

"""Portfolio analytics are calculated from saved outcomes without changing them."""
import tempfile
import unittest
from pathlib import Path

from app.portfolio import PaperPortfolio


class PortfolioAnalyticsTests(unittest.TestCase):
    def test_metrics_match_settled_and_open_position_history(self):
        with tempfile.TemporaryDirectory() as folder:
            portfolio = PaperPortfolio(Path(folder) / "portfolio.sqlite3")
            positions = [
                ("YES-WIN", "yes", 2, .2, .4),
                ("NO-LOSS", "no", 1, .3, .3),
                ("YES-LOSS", "yes", 2, .4, .8),
                ("PENDING", "yes", 1, .2, .2),
                ("REVIEW", "no", 1, .2, .2),
            ]
            for ticker, side, quantity, price, cost in positions:
                portfolio.add_order({"ticker":ticker, "title":ticker, "side":side,
                                     "quantity":quantity, "price":price, "contract_cost":cost,
                                     "max_loss":cost})

            portfolio.update_market("YES-WIN", "finalized", "yes")
            portfolio.update_market("NO-LOSS", "finalized", "yes")
            portfolio.update_market("YES-LOSS", "finalized", "no")
            portfolio.update_market("REVIEW", "finalized", "scalar")

            snapshot = portfolio.snapshot()
            self.assertEqual(snapshot["analytics"], {
                "win_rate": .3333,
                "settled_positions": 3,
                "open_positions": 2,
                "average_realized_pnl": .17,
                "total_realized_pnl": .5,
                "prediction_accuracy": .3333,
            })
            self.assertEqual(snapshot["realized_pnl"], snapshot["analytics"]["total_realized_pnl"])
            self.assertEqual({order["ticker"]: order["outcome"] for order in snapshot["orders"]}, {
                "YES-WIN":"won", "NO-LOSS":"lost", "YES-LOSS":"lost",
                "PENDING":"pending", "REVIEW":"review_required",
            })

    def test_rate_and_average_are_unavailable_without_settled_positions(self):
        with tempfile.TemporaryDirectory() as folder:
            portfolio = PaperPortfolio(Path(folder) / "portfolio.sqlite3")
            snapshot = portfolio.snapshot()
        analytics = snapshot["analytics"]
        self.assertIsNone(analytics["win_rate"])
        self.assertIsNone(analytics["prediction_accuracy"])
        self.assertIsNone(analytics["average_realized_pnl"])
        self.assertEqual((analytics["settled_positions"], analytics["open_positions"],
                          analytics["total_realized_pnl"]), (0, 0, 0.0))


if __name__ == "__main__":
    unittest.main()

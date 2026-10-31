"""Restart/recovery coverage for persistent paper portfolios and tracking."""
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.portfolio import OutcomeTracker, PaperPortfolio


class RestartRecoveryTests(unittest.TestCase):
    def test_portfolio_and_settlement_tracking_resume_after_restart(self):
        class RestartedDemoProvider:
            source = "kalshi_demo"

            def __init__(self):
                self.calls = []
                self.calls_lock = threading.Lock()
                self.both_checked = threading.Event()
                self.settle_second_position = False

            def get_market(self, ticker):
                with self.calls_lock:
                    self.calls.append(ticker)
                    if len(self.calls) >= 2:
                        self.both_checked.set()
                if ticker == "KXRECOVERY-YES":
                    return {"ticker": ticker, "status": "finalized", "result": "yes"}
                if self.settle_second_position:
                    return {"ticker": ticker, "status": "finalized", "result": "no"}
                return {"ticker": ticker, "status": "closed", "result": None}

        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "persistent-paper.sqlite3"

            # First application instance creates and persists two simulated positions.
            first_run = PaperPortfolio(database, starting_balance=1000.0)
            first_run.add_order({
                "ticker": "KXRECOVERY-YES", "title": "Persistent Yes position", "side": "yes",
                "quantity": 4, "price": .25, "contract_cost": 1.0, "fee": .04,
                "fee_type": "taker", "fee_status": "estimated", "fee_note": "fee estimate",
                "max_loss": 1.04,
            })
            first_run.add_order({
                "ticker": "KXRECOVERY-NO", "title": "Persistent No position", "side": "no",
                "quantity": 2, "price": .4, "contract_cost": .8, "fee": .02,
                "fee_type": "maker", "fee_status": "estimated", "fee_note": "fee estimate",
                "max_loss": .82,
            })
            before_restart = first_run.snapshot()
            self.assertEqual(before_restart["cash"], 998.14)
            self.assertEqual(before_restart["pending_count"], 2)
            original_orders = {order["ticker"]: order for order in before_restart["orders"]}
            original_created_at = {ticker: order["created_at"] for ticker, order in original_orders.items()}

            # Simulate application shutdown/startup: discard the old in-memory
            # portfolio and load a fresh instance from the same persistent file.
            first_run = None
            restarted_portfolio = PaperPortfolio(database, starting_balance=1000.0)
            after_restart = restarted_portfolio.snapshot()
            reloaded_orders = {order["ticker"]: order for order in after_restart["orders"]}
            self.assertEqual(after_restart["cash"], 998.14)
            self.assertEqual(after_restart["pending_count"], 2)
            self.assertEqual(set(reloaded_orders), {"KXRECOVERY-YES", "KXRECOVERY-NO"})
            for ticker, order in original_orders.items():
                reloaded = reloaded_orders[ticker]
                self.assertEqual(reloaded["created_at"], original_created_at[ticker])
                self.assertEqual((reloaded["side"], reloaded["quantity"], reloaded["max_loss"]),
                                 (order["side"], order["quantity"], order["max_loss"]))
                self.assertEqual(reloaded["outcome"], "pending")

            # A newly started tracker immediately checks the restored pending
            # tickers: one resolves and one stays pending until a later check.
            provider = RestartedDemoProvider()
            tracker = OutcomeTracker(restarted_portfolio, provider, interval_seconds=300)
            tracker.start()
            try:
                self.assertTrue(provider.both_checked.wait(2), "Restarted tracker did not check pending positions")
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    snapshot = restarted_portfolio.snapshot()
                    current = {order["ticker"]: order for order in snapshot["orders"]}
                    if current["KXRECOVERY-YES"]["outcome"] == "won":
                        break
                    time.sleep(.01)

                self.assertEqual(provider.calls.count("KXRECOVERY-YES"), 1)
                self.assertEqual(provider.calls.count("KXRECOVERY-NO"), 1)
                self.assertEqual(snapshot["cash"], 1002.14)
                self.assertEqual(snapshot["realized_pnl"], 2.96)
                self.assertEqual(snapshot["pending_count"], 1)
                self.assertEqual(current["KXRECOVERY-YES"]["outcome"], "won")
                self.assertEqual(current["KXRECOVERY-YES"]["payout"], 4.0)
                self.assertEqual(current["KXRECOVERY-NO"]["outcome"], "pending")
                self.assertEqual(current["KXRECOVERY-NO"]["max_loss"], .82)

                # A subsequent recovery poll can settle the remaining position;
                # prior realized accounting and both historical rows remain intact.
                provider.settle_second_position = True
                tracker.refresh_once()
                recovered = restarted_portfolio.snapshot()
                final_orders = {order["ticker"]: order for order in recovered["orders"]}
                self.assertEqual(recovered["cash"], 1004.14)
                self.assertEqual(recovered["realized_pnl"], 4.14)
                self.assertEqual(recovered["pending_count"], 0)
                self.assertEqual(len(final_orders), 2)
                self.assertEqual(final_orders["KXRECOVERY-NO"]["outcome"], "won")
                self.assertEqual(final_orders["KXRECOVERY-NO"]["payout"], 2.0)
                self.assertEqual(final_orders["KXRECOVERY-NO"]["realized_pnl"], 1.18)
            finally:
                tracker.stop()


if __name__ == "__main__":
    unittest.main()

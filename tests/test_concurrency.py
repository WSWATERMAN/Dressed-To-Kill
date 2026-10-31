"""Concurrent SQLite order-submission and idempotency regression tests."""
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from app.portfolio import PaperPortfolio


class ConcurrentOrderTests(unittest.TestCase):
    def test_simultaneous_orders_cannot_double_spend_cash(self):
        with tempfile.TemporaryDirectory() as folder:
            portfolio = PaperPortfolio(Path(folder) / "concurrent.sqlite3", starting_balance=1000.0)
            submissions = 20
            barrier = Barrier(submissions)

            def submit(index):
                order = {
                    "ticker": f"KXCONCURRENT-{index}", "title": f"Concurrent order {index}",
                    "side": "yes", "quantity": 100, "price": .5,
                    "contract_cost": 100.0, "fee": 0.0, "fee_type": "unavailable",
                    "fee_status": "unavailable", "max_loss": 100.0,
                }
                barrier.wait(timeout=5)
                try:
                    return ("saved", portfolio.add_order(order))
                except ValueError as error:
                    self.assertEqual(str(error), "Insufficient simulated cash")
                    return ("rejected", None)

            with ThreadPoolExecutor(max_workers=submissions) as pool:
                results = list(pool.map(submit, range(submissions)))

            saved = [result for state, result in results if state == "saved"]
            rejected = [state for state, _ in results if state == "rejected"]
            snapshot = portfolio.snapshot()
            self.assertEqual(len(saved), 10)
            self.assertEqual(len(rejected), 10)
            self.assertEqual(len(snapshot["orders"]), 10)
            self.assertEqual(snapshot["cash"], 0.0)
            self.assertEqual(sum(order["max_loss"] for order in snapshot["orders"]), 1000.0)

    def test_simultaneous_retries_with_same_client_id_create_one_order(self):
        with tempfile.TemporaryDirectory() as folder:
            portfolio = PaperPortfolio(Path(folder) / "idempotent.sqlite3", starting_balance=10.0)
            submissions = 20
            barrier = Barrier(submissions)
            order = {
                "ticker": "KXRETRY-1", "title": "Retry-safe order", "side": "no",
                "quantity": 5, "price": .25, "contract_cost": 1.25, "fee": .05,
                "fee_type": "taker", "fee_status": "estimated", "max_loss": 1.30,
                "client_order_id": "browser-request-unique-001",
            }

            def submit(_):
                barrier.wait(timeout=5)
                return portfolio.add_order(order)

            with ThreadPoolExecutor(max_workers=submissions) as pool:
                results = list(pool.map(submit, range(submissions)))

            snapshot = portfolio.snapshot()
            self.assertEqual(len({result["id"] for result in results}), 1)
            self.assertEqual(len(snapshot["orders"]), 1)
            self.assertEqual(snapshot["cash"], 8.7)
            self.assertEqual(snapshot["orders"][0]["client_order_id"], order["client_order_id"])

    def test_reusing_client_id_for_different_order_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            portfolio = PaperPortfolio(Path(folder) / "idempotent-mismatch.sqlite3", starting_balance=10.0)
            original = {
                "ticker": "KXRETRY-1", "title": "First request", "side": "yes",
                "quantity": 2, "price": .4, "max_loss": .8,
                "client_order_id": "same-request-id",
            }
            portfolio.add_order(original)
            altered = {**original, "quantity": 3, "max_loss": 1.2}
            with self.assertRaisesRegex(ValueError, "already used for a different order"):
                portfolio.add_order(altered)
            snapshot = portfolio.snapshot()
            self.assertEqual(len(snapshot["orders"]), 1)
            self.assertEqual(snapshot["cash"], 9.2)


if __name__ == "__main__":
    unittest.main()

"""Compatibility tests for additive upgrades of historical paper databases."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app.portfolio import PaperPortfolio


def deny_destructive_sqlite_actions(connection, actions):
    forbidden = {
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_TRIGGER,
    }
    for optional_name in ("SQLITE_DROP_TEMP_TABLE", "SQLITE_DROP_TEMP_INDEX", "SQLITE_DROP_TEMP_TRIGGER"):
        action = getattr(sqlite3, optional_name, None)
        if action is not None:
            forbidden.add(action)

    def authorize(action, arg1, arg2, database, trigger):
        if action in forbidden:
            actions.append((action, arg1, arg2))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)


class MigrationCompatibilityTests(unittest.TestCase):
    def create_pre_settlement_database(self, path):
        with closing(sqlite3.connect(path)) as connection:
            with connection as db:
                db.execute("""CREATE TABLE paper_orders (
                id TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                title TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('yes','no')),
                quantity INTEGER NOT NULL,
                price REAL NOT NULL,
                max_loss REAL NOT NULL,
                created_at TEXT NOT NULL
                )""")
                db.execute("""INSERT INTO paper_orders
                (id,ticker,title,side,quantity,price,max_loss,created_at)
                VALUES ('legacy-core','KXOLD-1','Historical yes position','yes',4,.25,1.0,
                            '2024-01-01T00:00:00+00:00')""")

    def create_pre_fee_database(self, path):
        with closing(sqlite3.connect(path)) as connection:
            with connection as db:
                db.execute("""CREATE TABLE paper_orders (
                id TEXT PRIMARY KEY,ticker TEXT NOT NULL,title TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('yes','no')),
                quantity INTEGER NOT NULL,price REAL NOT NULL,max_loss REAL NOT NULL,
                created_at TEXT NOT NULL,market_status TEXT NOT NULL DEFAULT 'unknown',
                result TEXT,outcome TEXT NOT NULL DEFAULT 'pending',payout REAL,
                realized_pnl REAL,last_checked_at TEXT,tracking_note TEXT
                )""")
                db.executemany("""INSERT INTO paper_orders
                (id,ticker,title,side,quantity,price,max_loss,created_at,market_status,
                 result,outcome,payout,realized_pnl)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", [
                ("fee-era-won", "KXOLD-2", "Historical settled position", "yes", 2, .25, .5,
                 "2024-02-01T00:00:00+00:00", "finalized", "yes", "won", 2.0, 1.5),
                ("fee-era-pending", "KXOLD-3", "Historical pending position", "no", 3, .25, .75,
                 "2024-02-02T00:00:00+00:00", "closed", None, "pending", None, None),
                ])

    def upgrade_without_destructive_operations(self, path):
        actions = []
        real_connect = sqlite3.connect

        def guarded_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            deny_destructive_sqlite_actions(connection, actions)
            return connection

        with patch("app.portfolio.sqlite3.connect", side_effect=guarded_connect):
            portfolio = PaperPortfolio(path)
        self.assertEqual(actions, [], "Migration attempted DELETE or DROP operations")
        return portfolio

    def test_pre_settlement_schema_upgrades_and_remains_operational(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "pre_settlement.sqlite3"
            self.create_pre_settlement_database(path)

            portfolio = self.upgrade_without_destructive_operations(path)
            order = portfolio.snapshot()["orders"][0]
            self.assertEqual(order["id"], "legacy-core")
            self.assertEqual(order["ticker"], "KXOLD-1")
            self.assertEqual((order["market_status"], order["outcome"]), ("unknown", "pending"))
            self.assertEqual((order["contract_cost"], order["fee"], order["fee_status"]),
                             (1.0, 0.0, "unavailable"))
            self.assertEqual(portfolio.snapshot()["cash"], 999.0)
            self.assertEqual(portfolio.snapshot()["realized_pnl"], 0.0)

            # Existing application operations still work after upgrading the old schema.
            portfolio.update_market("KXOLD-1", "finalized", "yes")
            self.assertEqual(portfolio.snapshot()["cash"], 1003.0)
            self.assertEqual(portfolio.snapshot()["orders"][0]["realized_pnl"], 3.0)
            portfolio.add_order({"ticker":"KXNEW-1", "title":"New position", "side":"no",
                                 "quantity":1, "price":.2, "max_loss":.2})
            self.assertEqual(len(portfolio.snapshot()["orders"]), 2)

    def test_pre_fee_schema_preserves_completed_and_pending_history_and_balance(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "pre_fee.sqlite3"
            self.create_pre_fee_database(path)

            portfolio = self.upgrade_without_destructive_operations(path)
            snapshot = portfolio.snapshot()
            orders = {order["id"]: order for order in snapshot["orders"]}
            self.assertEqual(set(orders), {"fee-era-won", "fee-era-pending"})
            self.assertEqual((orders["fee-era-won"]["outcome"], orders["fee-era-won"]["payout"],
                              orders["fee-era-won"]["realized_pnl"]), ("won", 2.0, 1.5))
            self.assertEqual((orders["fee-era-pending"]["outcome"], orders["fee-era-pending"]["payout"]),
                             ("pending", None))
            self.assertEqual((orders["fee-era-won"]["contract_cost"], orders["fee-era-pending"]["contract_cost"]),
                             (.5, .75))
            self.assertEqual(snapshot["cash"], 1000.75)
            self.assertEqual(snapshot["realized_pnl"], 1.5)

            # Reopening performs an idempotent migration and retains the same rows/balance.
            reopened = self.upgrade_without_destructive_operations(path)
            self.assertEqual(len(reopened.snapshot()["orders"]), 2)
            self.assertEqual(reopened.snapshot()["cash"], 1000.75)
            self.assertEqual(reopened.snapshot()["realized_pnl"], 1.5)


if __name__ == "__main__":
    unittest.main()

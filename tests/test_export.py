"""Streaming CSV portfolio export endpoint coverage."""
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app.server as server
from app.portfolio import PaperPortfolio


class PortfolioExportTests(unittest.TestCase):
    def request_export(self, portfolio, export_type):
        output = io.BytesIO()
        response = {"headers": []}
        handler = SimpleNamespace(
            path=f"/api/portfolio/export?type={export_type}",
            wfile=output,
            close_connection=False,
            send_response=lambda status: response.update(status=status),
            send_header=lambda name, value: response["headers"].append((name, value)),
            end_headers=lambda: None,
            _json=lambda status, payload: response.update(status=status, json=payload),
        )
        with patch.object(server, "STORE", portfolio):
            server.Handler.do_GET(handler)
        response["body"] = output.getvalue()
        return response

    def setUp_portfolio(self, path):
        portfolio = PaperPortfolio(path)
        portfolio.add_order({"ticker":"YES-1", "title":"Question, with \"quotes\"\nand another line",
                             "side":"yes", "quantity":2, "price":.2, "max_loss":.4})
        portfolio.add_order({"ticker":"NO-1", "title":"Losing question", "side":"no",
                             "quantity":1, "price":.3, "max_loss":.3})
        portfolio.add_order({"ticker":"OPEN-1", "title":"Open question", "side":"yes",
                             "quantity":1, "price":.25, "max_loss":.25})
        portfolio.update_market("YES-1", "finalized", "yes")
        portfolio.update_market("NO-1", "finalized", "yes")
        return portfolio

    def test_exports_positions_settlements_and_realized_pnl_records(self):
        with tempfile.TemporaryDirectory() as folder:
            portfolio = self.setUp_portfolio(Path(folder) / "portfolio.sqlite3")
            positions = self.request_export(portfolio, "positions")
            settlements = self.request_export(portfolio, "settlements")
            realized = self.request_export(portfolio, "realized-pnl")

        for response, filename in ((positions, "positions"), (settlements, "settlements"),
                                   (realized, "realized-pnl")):
            self.assertEqual(response["status"], 200)
            headers = dict(response["headers"])
            self.assertEqual(headers["Content-Type"], "text/csv; charset=utf-8")
            self.assertIn(f'filename="kalshi-{filename}.csv"', headers["Content-Disposition"])
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertTrue(response["body"].endswith(b"\r\n"))

        position_rows = list(csv.DictReader(io.StringIO(positions["body"].decode("utf-8"), newline="")))
        self.assertEqual(len(position_rows), 3)
        by_ticker = {row["ticker"]: row for row in position_rows}
        self.assertEqual(by_ticker["YES-1"]["title"], "Question, with \"quotes\"\nand another line")
        settlement_rows = list(csv.DictReader(io.StringIO(settlements["body"].decode("utf-8"), newline="")))
        self.assertEqual({row["ticker"] for row in settlement_rows}, {"YES-1", "NO-1"})
        pnl_rows = list(csv.DictReader(io.StringIO(realized["body"].decode("utf-8"), newline="")))
        self.assertEqual({row["ticker"] for row in pnl_rows}, {"YES-1", "NO-1"})
        self.assertEqual(sum(float(row["realized_pnl"]) for row in pnl_rows), 1.3)

    def test_invalid_export_type_returns_json_error(self):
        with tempfile.TemporaryDirectory() as folder:
            response = self.request_export(PaperPortfolio(Path(folder) / "portfolio.sqlite3"), "unknown")
        self.assertEqual(response["status"], 400)
        self.assertIn("error", response["json"])

    def test_large_export_returns_every_record_in_bounded_batches(self):
        with tempfile.TemporaryDirectory() as folder:
            portfolio = PaperPortfolio(Path(folder) / "portfolio.sqlite3")
            with portfolio._connect() as db:
                db.executemany(
                    "INSERT INTO paper_orders (id,ticker,title,side,quantity,price,max_loss,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    ((f"id-{index:04d}", f"KX-{index:04d}", f"Position {index}", "yes", 1, .01, .01,
                      "2026-01-01T00:00:00+00:00") for index in range(1205)),
                )
            records = list(portfolio.iter_export_rows("positions", batch_size=73))
            self.assertEqual(len(records), 1205)
            self.assertEqual(records[0]["ticker"], "KX-0000")
            self.assertEqual(records[-1]["ticker"], "KX-1204")
            response = self.request_export(portfolio, "positions")
            exported = list(csv.DictReader(io.StringIO(response["body"].decode("utf-8"), newline="")))
            self.assertEqual(len(exported), 1205)
            self.assertEqual(exported[-1]["ticker"], "KX-1204")


if __name__ == "__main__":
    unittest.main()

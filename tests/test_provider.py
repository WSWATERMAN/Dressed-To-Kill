import json
import threading
import time
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from app.provider import CachedProvider, DEMO_BASE, KalshiDemoProvider, MockProvider, ProviderError, ensure_demo_url, make_provider, normalize
from app.portfolio import OutcomeTracker, PaperPortfolio
from app.server import Handler, validate_paper_order
from app.fees import estimate_fee
from decimal import Decimal


class ProviderTests(unittest.TestCase):
    def test_mock_provider_is_default_and_deterministic(self):
        provider = make_provider({})
        self.assertIsInstance(provider, CachedProvider)
        self.assertIsInstance(provider.provider, MockProvider)
        self.assertEqual(provider.list_markets(), provider.list_markets())

    def test_mock_market_list_is_cached_until_expiration(self):
        class CountingMock(MockProvider):
            def __init__(self): self.calls = 0
            def list_markets(self, status="open"):
                self.calls += 1
                return super().list_markets(status)

        now = [0.0]
        provider = CountingMock()
        cached = CachedProvider(provider, ttl_seconds=5, clock=lambda: now[0])
        first = cached.list_markets()
        first[0].title = "caller mutation"
        self.assertNotEqual(cached.list_markets()[0].title, "caller mutation")
        self.assertEqual(provider.calls, 1)
        now[0] = 5.01
        cached.list_markets()
        self.assertEqual(provider.calls, 2)

    def test_large_market_list_can_be_returned_incrementally(self):
        first_page_ready = threading.Event()
        continue_loading = threading.Event()

        class IncrementalProvider:
            source = "kalshi_demo"
            def iter_market_pages(self, status):
                yield [normalize({"ticker":"PAGE-1", "status":"open"}, self.source)]
                first_page_ready.set()
                continue_loading.wait(2)
                yield [normalize({"ticker":"PAGE-2", "status":"open"}, self.source)]

        cached = CachedProvider(IncrementalProvider(), ttl_seconds=30)
        markets, loading, error = cached.market_list_snapshot("open")
        self.assertEqual((markets, loading, error), ([], True, None))
        self.assertTrue(first_page_ready.wait(1))
        markets, loading, error = cached.market_list_snapshot("open")
        self.assertEqual(([market.ticker for market in markets], loading, error), (["PAGE-1"], True, None))
        continue_loading.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            markets, loading, error = cached.market_list_snapshot("open")
            if not loading:
                break
            time.sleep(.01)
        self.assertIsNone(error)
        self.assertEqual([market.ticker for market in markets], ["PAGE-1", "PAGE-2"])

    def test_market_list_delta_returns_only_unseen_rows_and_progress(self):
        class SmallProvider:
            source = "mock"
            def list_markets(self, status="open"):
                return [normalize({"ticker":f"DELTA-{index}", "status":"open"}, self.source)
                        for index in range(3)]

        cached = CachedProvider(SmallProvider(), ttl_seconds=30)
        cached.list_markets("open")
        rows, count, loading, error = cached.market_list_delta("open", 1)
        self.assertEqual(([market.ticker for market in rows], count, loading, error),
                         (["DELTA-1", "DELTA-2"], 3, False, None))

    def test_markets_progress_route_returns_incremental_csv_safe_json(self):
        class ProgressProvider:
            source = "kalshi_demo"
            def market_list_delta(self, status, offset):
                self.assertion = (status, offset)
                return [normalize({"ticker":"PAGE-1", "status":"open"}, self.source)], 1500, True, None

        provider = ProgressProvider()
        response = {}
        handler = SimpleNamespace(path="/api/markets/progress?status=open&offset=1000",
                                  _json=lambda status, payload: response.update(status=status, payload=payload))
        from app import server as application_server
        with patch.object(application_server, "PROVIDER", provider):
            Handler.do_GET(handler)
        self.assertEqual(provider.assertion, ("open", 1000))
        self.assertEqual(response["status"], 200)
        self.assertEqual((response["payload"]["market_count"], response["payload"]["loading"]), (1500, True))
        self.assertEqual(response["payload"]["markets"][0]["ticker"], "PAGE-1")

    def test_demo_market_lookup_cache_reuses_and_expires_records(self):
        provider = KalshiDemoProvider()
        payloads = [
            b'{"markets":[{"ticker":"ABC-1","status":"active","result":"","yes_bid_dollars":"0.42"}]}',
            b'{"markets":[{"ticker":"ABC-1","status":"finalized","result":"yes","yes_bid_dollars":"1.00"}]}',
        ]
        responses = [type("Response", (), {"__enter__":lambda self:self, "__exit__":lambda *args:None,
                                             "read":lambda self,n,payload=body:payload})() for body in payloads]
        now = [0.0]
        cached = CachedProvider(provider, ttl_seconds=10, clock=lambda: now[0])
        with patch("app.provider.urlopen", side_effect=responses) as fetch:
            self.assertEqual(cached.get_markets(["ABC-1"])["ABC-1"]["status"], "active")
            self.assertEqual(cached.get_market("ABC-1")["status"], "active")
            self.assertEqual(fetch.call_count, 1)
            now[0] = 10.01
            self.assertEqual(cached.get_markets(["ABC-1"])["ABC-1"]["status"], "finalized")
            self.assertEqual(fetch.call_count, 2)

    def test_unknown_provider_fails(self):
        with self.assertRaises(ValueError):
            make_provider({"KALSHI_PROVIDER": "live"})

    def test_demo_url_is_exact_and_production_rejected(self):
        self.assertEqual(ensure_demo_url(DEMO_BASE), DEMO_BASE)
        self.assertEqual(ensure_demo_url("https://demo-api.kalshi.co/trade-api/v2"), "https://external-api.demo.kalshi.co/trade-api/v2")
        for url in ("https://external-api.kalshi.com/trade-api/v2", "https://api.elections.kalshi.com/trade-api/v2", "http://demo-api.kalshi.co/trade-api/v2", "https://demo-api.kalshi.co.evil.test/trade-api/v2", "https://demo-api.kalshi.co/other"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ensure_demo_url(url)

    def test_market_normalization_validates_and_parses(self):
        market = normalize({"ticker":"ABC-1", "title":"Question", "yes_bid_dollars":"0.31", "no_ask_dollars":"0.71", "volume_fp":"12.5"}, "demo")
        self.assertEqual((market.ticker, market.yes_bid, market.no_ask, market.volume), ("ABC-1", .31, .71, 12.5))
        with self.assertRaises(ProviderError):
            normalize({"title":"Missing ticker"}, "demo")

    def test_bad_remote_payload_is_reported(self):
        provider = KalshiDemoProvider()
        response = type("Response", (), {"__enter__": lambda self:self, "__exit__":lambda *args:None, "read":lambda self,n:b'{"markets":false}'})()
        with patch("app.provider.urlopen", return_value=response):
            with self.assertRaises(ProviderError):
                provider.list_markets()

    def test_demo_market_list_follows_cursors_and_preserves_filters(self):
        provider = KalshiDemoProvider()
        pages = [
            {"markets": [{"ticker": f"ABC-{i}", "status": "open"} for i in range(100)], "cursor": "next page/+"},
            {"markets": [{"ticker": f"ABC-{i}", "status": "open"} for i in range(100, 105)], "cursor": ""},
        ]
        responses = [
            type("Response", (), {
                "__enter__": lambda self: self,
                "__exit__": lambda *args: None,
                "read": lambda self, n, payload=json.dumps(page).encode(): payload,
            })()
            for page in pages
        ]
        with patch("app.provider.urlopen", side_effect=responses) as fetch:
            markets = provider.list_markets("open")

        self.assertEqual(len(markets), 105)
        self.assertEqual(markets[0].ticker, "ABC-0")
        self.assertEqual(markets[-1].ticker, "ABC-104")
        self.assertEqual(fetch.call_count, 2)
        first_query = parse_qs(urlsplit(fetch.call_args_list[0].args[0].full_url).query)
        second_query = parse_qs(urlsplit(fetch.call_args_list[1].args[0].full_url).query)
        self.assertEqual(first_query, {"limit": ["1000"], "mve_filter": ["exclude"], "status": ["open"]})
        self.assertEqual(second_query["cursor"], ["next page/+"])
        self.assertEqual(second_query["status"], ["open"])

    def test_demo_market_list_rejects_repeated_cursor(self):
        provider = KalshiDemoProvider()
        payload = json.dumps({"markets": [], "cursor": "same"}).encode()
        responses = [
            type("Response", (), {"__enter__": lambda self:self, "__exit__":lambda *args:None,
                                   "read":lambda self,n,payload=payload:payload})()
            for _ in range(2)
        ]
        with patch("app.provider.urlopen", side_effect=responses):
            with self.assertRaisesRegex(ProviderError, "repeated markets cursor"):
                provider.list_markets()

    def test_get_market_reads_demo_status_and_result(self):
        provider = KalshiDemoProvider()
        response = type("Response", (), {"__enter__": lambda self:self, "__exit__":lambda *args:None, "read":lambda self,n:b'{"markets":[{"ticker":"ABC-1","status":"settled","result":"YES"}]}'})()
        with patch("app.provider.urlopen", return_value=response) as request:
            self.assertEqual(provider.get_market("ABC-1"), {"ticker":"ABC-1", "status":"settled", "result":"yes", "yes_bid":None, "no_bid":None})
            full_url = request.call_args.args[0].full_url
            self.assertIn("/markets?", full_url)
            self.assertEqual(parse_qs(urlsplit(full_url).query)["tickers"], ["ABC-1"])

    def test_demo_batch_lookup_uses_one_request_for_multiple_tickers(self):
        provider = KalshiDemoProvider()
        response = type("Response", (), {
            "__enter__": lambda self:self,
            "__exit__": lambda *args:None,
            "read": lambda self,n:b'{"markets":[{"ticker":"ABC-1","status":"finalized","result":"yes","yes_bid_dollars":"1.00"},{"ticker":"XYZ-2","status":"closed","result":"","no_bid_dollars":"0.61"}]}',
        })()
        with patch("app.provider.urlopen", return_value=response) as fetch:
            markets = provider.get_markets(["ABC-1", "XYZ-2", "ABC-1"])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(set(markets), {"ABC-1", "XYZ-2"})
        self.assertEqual((markets["ABC-1"]["status"], markets["ABC-1"]["result"]), ("finalized", "yes"))
        self.assertEqual((markets["XYZ-2"]["status"], markets["XYZ-2"]["no_bid"]), ("closed", .61))
        query = parse_qs(urlsplit(fetch.call_args.args[0].full_url).query)
        self.assertEqual(query["tickers"], ["ABC-1,XYZ-2"])

    def test_demo_batch_lookup_chunks_more_than_fifty_tickers(self):
        provider = KalshiDemoProvider()
        responses = [
            type("Response", (), {"__enter__":lambda self:self, "__exit__":lambda *args:None,
                                  "read":lambda self,n:b'{"markets":[]}'})()
            for _ in range(2)
        ]
        tickers = [f"KX-{index}" for index in range(51)]
        with patch("app.provider.urlopen", side_effect=responses) as fetch:
            self.assertEqual(provider.get_markets(tickers), {})
        self.assertEqual(fetch.call_count, 2)
        batch_sizes = [len(parse_qs(urlsplit(call.args[0].full_url).query)["tickers"][0].split(","))
                       for call in fetch.call_args_list]
        self.assertEqual(batch_sizes, [50, 1])

    def test_paper_order_safeguards(self):
        order = validate_paper_order({"ticker":"ABC-1", "title":"Question", "side":"yes", "quantity":4, "price":.25, "fee_type":"taker", "client_order_id":"order-valid-001"}, 10)
        self.assertEqual(order["contract_cost"], 1)
        self.assertEqual(order["fee"], .06)
        self.assertEqual(order["max_loss"], 1.06)
        dotted = validate_paper_order({"ticker":"KXGOLDH-26OCT3016-T3881.99", "title":"Gold close price", "side":"no", "quantity":10, "price":.99, "fee_type":"maker", "client_order_id":"order-valid-002"}, 100)
        self.assertEqual(dotted["ticker"], "KXGOLDH-26OCT3016-T3881.99")
        for payload, cash in [
            ({"ticker":"ABC-1", "title":"Question", "side":"maybe", "quantity":1, "price":.3}, 10),
            ({"ticker":"ABC-1", "title":"Question", "side":"yes", "quantity":1001, "price":.3}, 1000),
            ({"ticker":"ABC-1", "title":"Question", "side":"yes", "quantity":4, "price":.25, "fee_type":"taker"}, 1.05),
            ({"ticker":"bad/ticker", "title":"Question", "side":"yes", "quantity":1, "price":.2}, 10),
        ]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_paper_order(payload, cash)
        with self.assertRaisesRegex(ValueError, "client order ID"):
            validate_paper_order({"ticker":"ABC-1", "title":"Question", "side":"yes", "quantity":1, "price":.2}, 10)

    def test_kalshi_fee_formulas_and_rounding(self):
        self.assertEqual(estimate_fee("ABC-1", Decimal("0.50"), 100, "taker")["fee"], Decimal("1.75"))
        self.assertEqual(estimate_fee("ABC-1", Decimal("0.50"), 100, "maker")["fee"], Decimal("0.44"))
        self.assertEqual(estimate_fee("ABC-1", Decimal("0.50"), 1, "taker")["fee"], Decimal("0.02"))
        self.assertEqual(estimate_fee("ABC-1", Decimal("0.50"), 1, "maker")["fee"], Decimal("0.01"))

    def test_special_market_fee_is_unavailable_and_inputs_validated(self):
        self.assertIsNone(estimate_fee("INX-TEST", Decimal("0.5"), 10, "taker")["fee"])
        with self.assertRaises(ValueError): estimate_fee("ABC", Decimal("NaN"), 10, "taker")
        with self.assertRaises(ValueError): estimate_fee("ABC", Decimal("0.5"), 0, "maker")
        with self.assertRaises(ValueError): estimate_fee("ABC", Decimal("0.5"), 1, "unknown")

    def test_http_request_logging_avoids_reserved_record_fields(self):
        fake_handler = SimpleNamespace(client_address=("127.0.0.1", 58664))
        with self.assertLogs("kalshi_lab", level="INFO") as captured:
            Handler.log_message(fake_handler, '"%s" %s %s', "GET /api/health", "200", "2")
        self.assertIn("http_request", captured.output[0])

    def test_paper_orders_persist_and_settlement_updates_realized_pnl(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as folder:
            store = PaperPortfolio(Path(folder) / "portfolio.sqlite3")
            store.add_order({"ticker":"ABC-1", "title":"Question", "side":"yes", "quantity":4, "price":.25, "contract_cost":1.0, "fee":.04, "fee_type":"taker", "fee_status":"estimated", "fee_note":"estimate", "max_loss":1.04})
            reopened = PaperPortfolio(Path(folder) / "portfolio.sqlite3")
            self.assertEqual(reopened.pending_tickers(), ["ABC-1"])
            reopened.update_market("ABC-1", "settled", "yes")
            snapshot = reopened.snapshot(tracking_enabled=True)
            self.assertEqual(snapshot["cash"], 1002.96)
            self.assertEqual(snapshot["realized_pnl"], 2.96)
            self.assertEqual(snapshot["orders"][0]["outcome"], "won")
            self.assertTrue(snapshot["tracking_enabled"])

    def test_legacy_database_migrates_without_losing_rows(self):
        import tempfile, sqlite3
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"old.sqlite3"
            db=sqlite3.connect(path)
            db.execute("""CREATE TABLE paper_orders (id TEXT PRIMARY KEY,ticker TEXT NOT NULL,title TEXT NOT NULL,side TEXT NOT NULL,quantity INTEGER NOT NULL,price REAL NOT NULL,max_loss REAL NOT NULL,created_at TEXT NOT NULL,market_status TEXT NOT NULL DEFAULT 'unknown',result TEXT,outcome TEXT NOT NULL DEFAULT 'pending',payout REAL,realized_pnl REAL,last_checked_at TEXT,tracking_note TEXT)""")
            db.execute("INSERT INTO paper_orders(id,ticker,title,side,quantity,price,max_loss,created_at) VALUES('old','ABC','Old order','yes',2,.25,.5,'2026-01-01T00:00:00+00:00')")
            db.commit();db.close()
            store=PaperPortfolio(path)
            order=store.snapshot()["orders"][0]
            self.assertEqual((order["contract_cost"],order["fee"],order["fee_status"],order["max_loss"]),(.5,0.0,"unavailable",.5))

    def test_finalized_yes_pays_out_and_records_net_pnl(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as folder:
            store = PaperPortfolio(Path(folder) / "portfolio.sqlite3")
            store.add_order({"ticker":"ABC-1", "title":"Question", "side":"yes", "quantity":4, "price":.25, "max_loss":1.0})
            store.update_market("ABC-1", "finalized", "yes")
            order = store.snapshot()["orders"][0]
            self.assertEqual((order["market_status"], order["result"], order["outcome"]), ("finalized", "yes", "won"))
            self.assertEqual(order["payout"], 4.0)
            self.assertEqual(order["realized_pnl"], 3.0)
            self.assertEqual(store.snapshot()["cash"], 1003.0)

    def test_finalized_no_pays_out_no_side_and_loses_yes_side(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as folder:
            store = PaperPortfolio(Path(folder) / "portfolio.sqlite3")
            store.add_order({"ticker":"ABC-1", "title":"No position", "side":"no", "quantity":3, "price":.4, "max_loss":1.2})
            store.add_order({"ticker":"ABC-2", "title":"Yes position", "side":"yes", "quantity":2, "price":.3, "max_loss":.6})
            store.update_market("ABC-1", "finalized", "no")
            store.update_market("ABC-2", "finalized", "no")
            orders = {order["ticker"]: order for order in store.snapshot()["orders"]}

            self.assertEqual((orders["ABC-1"]["outcome"], orders["ABC-1"]["payout"], orders["ABC-1"]["realized_pnl"]), ("won", 3.0, 1.8))
            self.assertEqual((orders["ABC-2"]["outcome"], orders["ABC-2"]["payout"], orders["ABC-2"]["realized_pnl"]), ("lost", 0.0, -0.6))
            self.assertEqual(store.snapshot()["cash"], 1001.2)

    def test_unresolved_market_does_not_settle_even_with_result(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as folder:
            store = PaperPortfolio(Path(folder) / "portfolio.sqlite3")
            store.add_order({"ticker":"ABC-1", "title":"Question", "side":"yes", "quantity":2, "price":.3, "max_loss":.6})
            store.update_market("ABC-1", "determined", "yes")
            order = store.snapshot()["orders"][0]
            self.assertEqual((order["market_status"], order["outcome"]), ("determined", "pending"))
            self.assertIsNone(order["payout"])
            self.assertIsNone(order["realized_pnl"])
            self.assertEqual(store.snapshot()["cash"], 999.4)

    def test_finalized_missing_result_requires_review_without_accounting_change(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as folder:
            store = PaperPortfolio(Path(folder) / "portfolio.sqlite3")
            store.add_order({"ticker":"ABC-1", "title":"Question", "side":"yes", "quantity":2, "price":.3, "max_loss":.6})
            store.update_market("ABC-1", "finalized", None)
            order = store.snapshot()["orders"][0]
            self.assertEqual(order["outcome"], "review_required")
            self.assertIsNone(order["result"])
            self.assertIsNone(order["payout"])
            self.assertIsNone(order["realized_pnl"])
            self.assertIn("no result", order["tracking_note"])
            self.assertEqual(store.snapshot()["cash"], 999.4)

    def test_finalized_nonbinary_result_requires_review(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as folder:
            store = PaperPortfolio(Path(folder) / "portfolio.sqlite3")
            store.add_order({"ticker":"ABC-1", "title":"Question", "side":"yes", "quantity":1, "price":.5, "max_loss":.5})
            store.update_market("ABC-1", "finalized", "scalar")
            order = store.snapshot()["orders"][0]
            self.assertEqual(order["outcome"], "review_required")
            self.assertEqual(order["result"], "scalar")
            self.assertIn("Unsupported finalized result", order["tracking_note"])
            self.assertIsNone(order["payout"])
            self.assertIsNone(order["realized_pnl"])

    def test_outcome_tracker_uses_demo_settlement_response(self):
        import tempfile
        from pathlib import Path

        class SettledDemo:
            source = "kalshi_demo"
            def get_market(self, ticker):
                return {"ticker": ticker, "status": "settled", "result": "no"}

        with tempfile.TemporaryDirectory() as folder:
            store = PaperPortfolio(Path(folder) / "portfolio.sqlite3")
            store.add_order({"ticker":"ABC-1", "title":"Question", "side":"no", "quantity":2, "price":.4, "max_loss":.8})
            OutcomeTracker(store, SettledDemo()).refresh_once()
            order = store.snapshot()["orders"][0]
            self.assertEqual((order["outcome"], order["payout"], order["realized_pnl"]), ("won", 2.0, 1.2))

    def test_outcome_tracker_batches_pending_market_reads(self):
        import tempfile
        from pathlib import Path

        class BatchSettledDemo:
            source = "kalshi_demo"
            def __init__(self): self.calls = []
            def get_markets(self, tickers):
                self.calls.append(list(tickers))
                return {
                    "ABC-1": {"ticker":"ABC-1", "status":"finalized", "result":"yes"},
                    "ABC-2": {"ticker":"ABC-2", "status":"finalized", "result":"no"},
                }

        with tempfile.TemporaryDirectory() as folder:
            store = PaperPortfolio(Path(folder) / "portfolio.sqlite3")
            for ticker, side in (("ABC-1", "yes"), ("ABC-2", "no")):
                store.add_order({"ticker":ticker, "title":"Question", "side":side,
                                 "quantity":1, "price":.5, "max_loss":.5})
            provider = BatchSettledDemo()
            OutcomeTracker(store, provider).refresh_once()
            self.assertEqual(provider.calls, [["ABC-1", "ABC-2"]])
            self.assertEqual({o["ticker"]:o["outcome"] for o in store.snapshot()["orders"]},
                             {"ABC-1":"won", "ABC-2":"won"})

    def test_settlement_result_must_be_binary_yes_or_no(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as folder:
            store = PaperPortfolio(Path(folder) / "portfolio.sqlite3")
            store.add_order({"ticker":"ABC-1", "title":"Question", "side":"no", "quantity":2, "price":.5, "max_loss":1.0})
            store.update_market("ABC-1", "settled", "void")
            snapshot = store.snapshot()
            self.assertEqual(snapshot["orders"][0]["outcome"], "review_required")
            self.assertIsNone(snapshot["orders"][0]["realized_pnl"])

    def test_persistent_cash_limit_is_checked_at_insert(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as folder:
            store = PaperPortfolio(Path(folder) / "portfolio.sqlite3", starting_balance=1.0)
            order = {"ticker":"ABC-1", "title":"Question", "side":"yes", "quantity":2, "price":.5, "max_loss":1.0}
            store.add_order(order)
            with self.assertRaises(ValueError):
                store.add_order(order)

    def test_mock_provider_does_not_mark_mock_orders_settled(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as folder:
            store = PaperPortfolio(Path(folder) / "portfolio.sqlite3")
            store.add_order({"ticker":"ABC-1", "title":"Question", "side":"yes", "quantity":1, "price":.5, "max_loss":.5})
            OutcomeTracker(store, MockProvider()).refresh_once()
            self.assertEqual(store.snapshot()["orders"][0]["outcome"], "pending")


if __name__ == "__main__":
    unittest.main()

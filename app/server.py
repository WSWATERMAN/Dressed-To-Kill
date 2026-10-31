"""Small local HTTP app with a persistent paper account; no real orders."""
from __future__ import annotations

import json
import csv
import logging
import os
import re
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.portfolio import EXPORT_COLUMNS, OutcomeTracker, PaperPortfolio
from app.fees import estimate_fee
from app.provider import ProviderError, asdict, make_provider, valid_market_ticker

ROOT = Path(__file__).parent
LOG = logging.getLogger("kalshi_lab")
STORE: PaperPortfolio | None = None
TRACKER: OutcomeTracker | None = None
PROVIDER = None


class _Utf8Writer:
    """Minimal text sink that lets csv.writer stream encoded rows to a socket."""

    def __init__(self, binary_stream):
        self.binary_stream = binary_stream

    def write(self, value):
        encoded = value.encode("utf-8")
        self.binary_stream.write(encoded)
        return len(value)


def portfolio_snapshot():
    """Add best available side-specific bid estimates to the portfolio response."""
    if STORE is None:
        return None
    quotes = {}
    if PROVIDER is not None:
        tickers = STORE.pending_tickers()
        if callable(getattr(PROVIDER, "get_markets", None)) and tickers:
            try:
                markets = PROVIDER.get_markets(tickers)
                for ticker in tickers:
                    market = markets.get(ticker, {}) if isinstance(markets, dict) else {}
                    quotes[ticker] = {"yes_bid": market.get("yes_bid"), "no_bid": market.get("no_bid")}
            except (ProviderError, ValueError):
                # Market data gaps must not make the saved portfolio inaccessible.
                pass
        else:
            for ticker in tickers:
                try:
                    market = PROVIDER.get_market(ticker)
                    quotes[ticker] = {"yes_bid": market.get("yes_bid"), "no_bid": market.get("no_bid")}
                except (ProviderError, ValueError):
                    # Market data gaps must not make the saved portfolio inaccessible.
                    continue
    return STORE.snapshot(
        tracking_enabled=getattr(PROVIDER, "source", None) == "kalshi_demo",
        current_quotes=quotes,
    )


def validate_paper_order(body, cash):
    if not isinstance(body, dict):
        raise ValueError("Invalid order payload")
    ticker, side, title = body.get("ticker"), body.get("side"), body.get("title")
    raw_qty = body.get("quantity", 0)
    if isinstance(raw_qty, bool) or not str(raw_qty).isdigit():
        raise ValueError("Quantity must be a whole number")
    qty = int(raw_qty)
    client_order_id = body.get("client_order_id")
    if not isinstance(client_order_id, str) or re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", client_order_id) is None:
        raise ValueError("Invalid client order ID")
    try:
        price_decimal = Decimal(str(body.get("price", "-1")))
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid price") from None
    if not price_decimal.is_finite():
        raise ValueError("Invalid price")
    # Accept integer-cent values from market data integrations, while keeping
    # submitted decimal dollar values (including 1–less-than-100 cents) intact.
    if price_decimal == price_decimal.to_integral_value() and price_decimal > 1:
        price_decimal /= Decimal("100")
    if not valid_market_ticker(ticker):
        raise ValueError("Invalid market ticker")
    if not isinstance(title, str) or not title.strip() or len(title) > 500:
        raise ValueError("Invalid market title")
    if side not in {"yes", "no"} or not 1 <= qty <= 1000 or not 0 < price_decimal < 1:
        raise ValueError("Side, quantity, or price is out of range")
    fee_type = body.get("fee_type")
    fee_estimate = estimate_fee(ticker, price_decimal, qty, fee_type)
    cost = (price_decimal * qty).quantize(Decimal("0.01"))
    fee = fee_estimate["fee"] or Decimal("0.00")
    total_cost = cost + fee
    if float(total_cost) > cash:
        raise ValueError("Insufficient simulated cash")
    if fee_estimate["fee"] is not None and total_cost > Decimal(qty):
        raise ValueError("Total cost cannot exceed the maximum possible payout")
    return {"ticker": ticker, "title": title.strip(), "side": side, "quantity": qty,
            "price": float(price_decimal), "contract_cost": float(cost), "fee": float(fee),
            "fee_type": fee_type, "fee_status": fee_estimate["fee_status"],
            "fee_note": fee_estimate["fee_note"], "max_loss": float(total_cost),
            "client_order_id": client_order_id}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # `message` is a reserved LogRecord field; use request_line for the formatted HTTP detail.
        LOG.info("http_request", extra={"client": self.client_address[0], "request_line": fmt % args})

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            database_status = "ok"
            try:
                if STORE is None:
                    raise RuntimeError("Portfolio storage is not initialized")
                STORE.health_check()
            except Exception:
                # Keep diagnostics useful without leaking paths, SQL, or exception details.
                database_status = "error"
            configured_provider = getattr(PROVIDER, "source", None) or os.getenv("KALSHI_PROVIDER", "mock").strip().lower()
            if configured_provider not in {"mock", "kalshi_demo"}:
                configured_provider = "unknown"
            status = "ok" if database_status == "ok" else "error"
            return self._json(200 if status == "ok" else 503, {
                "status": status,
                "database": database_status,
                "provider": configured_provider or "unknown",
            })
        if parsed.path == "/api/health":
            return self._json(200, {"status": "ok"})
        if parsed.path == "/api/markets/progress":
            params = parse_qs(parsed.query)
            status = params.get("status", ["open"])[0]
            try:
                offset = int(params.get("offset", ["0"])[0])
                if offset < 0:
                    raise ValueError("Market offset must be non-negative")
                provider = PROVIDER if PROVIDER is not None else make_provider()
                progress = getattr(provider, "market_list_delta", None)
                if not callable(progress):
                    markets = provider.list_markets(status)
                    page, count, loading, error = markets[offset:offset + 1000], len(markets), False, None
                else:
                    page, count, loading, error = progress(status, offset)
                if error:
                    LOG.warning("market_provider_error", extra={"reason": error})
                    return self._json(502, {"error": error})
                source = page[0].source if page else getattr(provider, "source", os.getenv("KALSHI_PROVIDER", "mock"))
                return self._json(200, {"source": source, "markets": [asdict(m) for m in page],
                                        "market_count": count, "loading": loading})
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            except ProviderError as exc:
                LOG.warning("market_provider_error", extra={"reason": str(exc)})
                return self._json(502, {"error": str(exc)})
        if parsed.path == "/api/markets":
            params = parse_qs(parsed.query)
            status = params.get("status", ["open"])[0]
            try:
                provider = PROVIDER if PROVIDER is not None else make_provider()
                list_snapshot = getattr(provider, "market_list_snapshot", None)
                if callable(list_snapshot):
                    markets, loading, error = list_snapshot(status)
                    if error:
                        LOG.warning("market_provider_error", extra={"reason": error})
                        return self._json(502, {"error": error})
                else:
                    markets = provider.list_markets(status)
                    loading = False
                source = markets[0].source if markets else getattr(provider, "source", os.getenv("KALSHI_PROVIDER", "mock"))
                return self._json(200, {"source": source, "markets": [asdict(m) for m in markets], "loading": loading})
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            except ProviderError as exc:
                LOG.warning("market_provider_error", extra={"reason": str(exc)})
                return self._json(502, {"error": str(exc)})
        if parsed.path == "/api/portfolio":
            if STORE is None:
                return self._json(503, {"error": "Portfolio storage is not initialized"})
            return self._json(200, portfolio_snapshot())
        if parsed.path == "/api/portfolio/export":
            export_type = parse_qs(parsed.query).get("type", ["positions"])[0]
            if export_type not in EXPORT_COLUMNS:
                return self._json(400, {"error": "Export type must be positions, settlements, or realized-pnl"})
            if STORE is None:
                return self._json(503, {"error": "Portfolio storage is not initialized"})
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="kalshi-{export_type}.csv"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            writer = csv.writer(_Utf8Writer(self.wfile), lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
            columns = EXPORT_COLUMNS[export_type]
            writer.writerow(columns)
            for row in STORE.iter_export_rows(export_type):
                writer.writerow([row.get(column) for column in columns])
            return
        if parsed.path == "/" or parsed.path == "/index.html":
            content = (ROOT / "static" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            return self.wfile.write(content)
        if parsed.path in {"/app.js", "/styles.css"}:
            content = (ROOT / "static" / parsed.path.lstrip("/")).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript" if parsed.path.endswith(".js") else "text/css")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            return self.wfile.write(content)
        return self._json(404, {"error": "Not found"})

    def do_POST(self):
        if urlparse(self.path).path != "/api/paper-orders":
            return self._json(404, {"error": "Not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 10_000:
                raise ValueError("Invalid request size")
            body = json.loads(self.rfile.read(length))
            if STORE is None:
                return self._json(503, {"error": "Portfolio storage is not initialized"})
            order = validate_paper_order(body, STORE.snapshot()["cash"])
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return self._json(400, {"error": str(exc)})
        if STORE is None:
            return self._json(503, {"error": "Portfolio storage is not initialized"})
        try:
            saved_order = STORE.add_order(order)
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        if TRACKER is not None:
            TRACKER.wake()
        return self._json(201, {"order": saved_order, "portfolio": portfolio_snapshot()})


def main():
    global STORE, TRACKER, PROVIDER
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    host = os.getenv("APP_HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "localhost", "0.0.0.0"}:
        raise SystemExit("APP_HOST must be localhost or 127.0.0.1")
    port = int(os.getenv("APP_PORT", "8765"))
    STORE = PaperPortfolio()
    PROVIDER = make_provider()
    TRACKER = OutcomeTracker(STORE, PROVIDER, int(os.getenv("KALSHI_OUTCOME_POLL_SECONDS", "300")))
    TRACKER.start()
    server = ThreadingHTTPServer((host, port), Handler)
    LOG.info("server_started", extra={"host": host, "port": port, "provider": os.getenv("KALSHI_PROVIDER", "mock")})
    try:
        server.serve_forever()
    finally:
        server.server_close()
        TRACKER.stop()


if __name__ == "__main__":
    main()


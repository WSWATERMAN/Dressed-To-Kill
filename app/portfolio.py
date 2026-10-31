"""Persistent paper positions and demo-only settlement outcome tracking."""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from app.provider import ProviderError

LOG = logging.getLogger("kalshi_lab")
DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "paper_portfolio.sqlite3"
EXPORT_COLUMNS = {
    "positions": ("id", "ticker", "title", "side", "quantity", "price", "contract_cost", "fee",
                  "fee_type", "fee_status", "max_loss", "created_at", "market_status", "result",
                  "outcome", "payout", "realized_pnl", "last_checked_at", "tracking_note"),
    "settlements": ("id", "ticker", "title", "side", "quantity", "result", "outcome", "payout",
                    "realized_pnl", "market_status", "last_checked_at"),
    "realized-pnl": ("id", "ticker", "title", "side", "quantity", "result", "outcome",
                     "realized_pnl", "payout", "created_at", "last_checked_at"),
}

# REST lifecycle states documented by Kalshi. Only finalized is terminal in the
# current REST API; settled remains accepted for compatibility with older/demo
# responses and existing local records. All other known states are unresolved.
UNRESOLVED_MARKET_STATUSES = frozenset({
    "initialized", "active", "inactive", "closed", "determined", "disputed", "amended",
})
FINAL_MARKET_STATUSES = frozenset({"finalized", "settled"})
SUPPORTED_BINARY_RESULTS = frozenset({"yes", "no"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PaperPortfolio:
    def __init__(self, db_path: str | Path | None = None, starting_balance: float = 1000.0):
        self.db_path = Path(db_path or os.getenv("KALSHI_DB_PATH", str(DEFAULT_DB)))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.starting_balance = float(starting_balance)
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self):
        with self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS paper_orders (
                id TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                title TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('yes','no')),
                quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 1000),
                price REAL NOT NULL CHECK(price > 0 AND price < 1),
                contract_cost REAL NOT NULL DEFAULT 0,
                fee REAL NOT NULL DEFAULT 0,
                fee_type TEXT NOT NULL DEFAULT 'unavailable',
                fee_status TEXT NOT NULL DEFAULT 'unavailable',
                fee_note TEXT NOT NULL DEFAULT 'Fee was not recorded for this legacy paper order.',
                client_order_id TEXT,
                max_loss REAL NOT NULL,
                created_at TEXT NOT NULL,
                market_status TEXT NOT NULL DEFAULT 'unknown',
                result TEXT,
                outcome TEXT NOT NULL DEFAULT 'pending',
                payout REAL,
                realized_pnl REAL,
                last_checked_at TEXT,
                tracking_note TEXT
            )""")
            # Additive migrations preserve existing local portfolio rows. These
            # fields cover both pre-settlement and pre-fee database versions.
            columns = {row["name"] for row in db.execute("PRAGMA table_info(paper_orders)")}
            for name, declaration in (
                ("market_status", "TEXT NOT NULL DEFAULT 'unknown'"),
                ("result", "TEXT"),
                ("outcome", "TEXT NOT NULL DEFAULT 'pending'"),
                ("payout", "REAL"),
                ("realized_pnl", "REAL"),
                ("last_checked_at", "TEXT"),
                ("tracking_note", "TEXT"),
                ("contract_cost", "REAL NOT NULL DEFAULT 0"),
                ("fee", "REAL NOT NULL DEFAULT 0"),
                ("fee_type", "TEXT NOT NULL DEFAULT 'unavailable'"),
                ("fee_status", "TEXT NOT NULL DEFAULT 'unavailable'"),
                ("fee_note", "TEXT NOT NULL DEFAULT 'Fee was not recorded for this legacy paper order.'"),
                ("client_order_id", "TEXT"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE paper_orders ADD COLUMN {name} {declaration}")
            db.execute("UPDATE paper_orders SET contract_cost=max_loss WHERE contract_cost=0 AND fee_status='unavailable'")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_orders_client_order_id
                ON paper_orders(client_order_id) WHERE client_order_id IS NOT NULL""")

    def add_order(self, order: dict) -> dict:
        # Preserve compatibility for callers and old tests that construct orders
        # directly: treat their supplied max_loss as cost and fee as unavailable.
        order = {"contract_cost": order.get("contract_cost", order["max_loss"]),
                 "fee": order.get("fee", 0.0), "fee_type": order.get("fee_type", "unavailable"),
                 "fee_status": order.get("fee_status", "unavailable"),
                 "fee_note": order.get("fee_note", "Fee was not recorded for this legacy paper order."),
                 "client_order_id": order.get("client_order_id"), **order}
        order_id = str(uuid.uuid4())
        created_at = utc_now()
        with self._connect() as db:
            # Serialize concurrent paper orders before checking and reserving cash.
            db.execute("BEGIN IMMEDIATE")
            client_order_id = order.get("client_order_id")
            if client_order_id:
                existing = db.execute("SELECT * FROM paper_orders WHERE client_order_id=?", (client_order_id,)).fetchone()
                if existing is not None:
                    # An idempotent retry returns its original order. Reusing the
                    # key for a different position is rejected instead of silently
                    # returning or charging for a different request.
                    if any(existing[key] != order[key] for key in ("ticker", "side", "quantity", "price")):
                        raise ValueError("Client order ID is already used for a different order")
                    saved = dict(existing)
                    saved.pop("client_order_id", None)
                    return saved
            totals = db.execute("SELECT COALESCE(SUM(max_loss),0) AS spent, COALESCE(SUM(payout),0) AS paid FROM paper_orders").fetchone()
            available_cash = self.starting_balance - float(totals["spent"]) + float(totals["paid"])
            if float(order["max_loss"]) > available_cash:
                raise ValueError("Insufficient simulated cash")
            db.execute("""INSERT INTO paper_orders
                (id,ticker,title,side,quantity,price,contract_cost,fee,fee_type,fee_status,fee_note,
                 client_order_id,max_loss,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    order_id, order["ticker"], order["title"], order["side"],
                    order["quantity"], order["price"], order["contract_cost"], order["fee"],
                    order["fee_type"], order["fee_status"], order["fee_note"], client_order_id,
                    order["max_loss"], created_at,
                ))
        return {"id": order_id, **order, "created_at": created_at,
                "market_status": "unknown", "result": None, "outcome": "pending",
                "payout": None, "realized_pnl": None, "last_checked_at": None,
                "tracking_note": None}

    def pending_tickers(self) -> list[str]:
        with self._connect() as db:
            rows = db.execute("SELECT DISTINCT ticker FROM paper_orders WHERE outcome='pending'").fetchall()
        return [row["ticker"] for row in rows]

    def health_check(self) -> None:
        """Raise a database error if the configured SQLite store is unavailable."""
        with self._connect() as db:
            db.execute("SELECT 1").fetchone()

    def iter_export_rows(self, export_type: str, batch_size: int = 500):
        """Yield export records in bounded batches from the existing portfolio history."""
        columns = EXPORT_COLUMNS.get(export_type)
        if columns is None:
            raise ValueError("Unsupported portfolio export type")
        select = ",".join(columns)
        if export_type == "settlements":
            query = f"SELECT {select} FROM paper_orders WHERE outcome IN ('won','lost') ORDER BY last_checked_at,id"
        elif export_type == "realized-pnl":
            query = f"SELECT {select} FROM paper_orders WHERE realized_pnl IS NOT NULL ORDER BY last_checked_at,id"
        else:
            query = f"SELECT {select} FROM paper_orders ORDER BY created_at,id"
        with self._connect() as db:
            cursor = db.execute(query)
            while True:
                rows = cursor.fetchmany(max(1, int(batch_size)))
                if not rows:
                    break
                for row in rows:
                    yield dict(row)

    def update_market(self, ticker: str, status: str, result: str | None):
        """Apply terminal binary results; keep unsupported terminal results reviewable."""
        normalized_status = status.strip().lower() if isinstance(status, str) else "unknown"
        normalized_result = result.strip().lower()[:100] if isinstance(result, str) else None
        is_final = normalized_status in FINAL_MARKET_STATUSES
        checked_at = utc_now()
        with self._connect() as db:
            rows = db.execute("SELECT * FROM paper_orders WHERE ticker=? AND outcome='pending'", (ticker,)).fetchall()
            for row in rows:
                if is_final and normalized_result in SUPPORTED_BINARY_RESULTS:
                    payout = float(row["quantity"]) if row["side"] == normalized_result else 0.0
                    pnl = round(payout - float(row["max_loss"]), 2)
                    outcome = "won" if payout > 0 else "lost"
                    db.execute("""UPDATE paper_orders SET market_status=?,result=?,outcome=?,payout=?,
                        realized_pnl=?,last_checked_at=?,tracking_note=NULL WHERE id=?""", (
                        normalized_status, normalized_result, outcome, payout, pnl, checked_at, row["id"],
                    ))
                elif is_final:
                    # Unknown/void/non-binary outcomes must not be treated as a win or a loss.
                    result_note = (
                        f"Unsupported finalized result '{normalized_result}'; review contract settlement."
                        if normalized_result else
                        "Finalized market response has no result; review contract settlement."
                    )
                    db.execute("""UPDATE paper_orders SET market_status=?,result=?,outcome='review_required',
                        last_checked_at=?,tracking_note=? WHERE id=?""", (
                        normalized_status, normalized_result, checked_at,
                        result_note, row["id"],
                    ))
                else:
                    db.execute("""UPDATE paper_orders SET market_status=?,last_checked_at=?,tracking_note=NULL
                        WHERE id=?""", (normalized_status, checked_at, row["id"]))

    def record_tracking_error(self, ticker: str, error: str):
        # Store only a short generic error string; never save URLs or request headers.
        with self._connect() as db:
            db.execute("""UPDATE paper_orders SET last_checked_at=?,tracking_note=?
                WHERE ticker=? AND outcome='pending'""", (utc_now(), error[:200], ticker))

    def snapshot(self, tracking_enabled: bool = False, current_quotes: dict | None = None) -> dict:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM paper_orders ORDER BY created_at DESC,id DESC").fetchall()
        orders = [dict(row) for row in rows]
        spent = sum(float(order["max_loss"]) for order in orders)
        paid_out = sum(float(order["payout"] or 0) for order in orders)
        realized = round(sum(float(order["realized_pnl"] or 0) for order in orders), 2)
        settled_orders = [order for order in orders if order["outcome"] in {"won", "lost"}]
        pnl_orders = [order for order in settled_orders if order["realized_pnl"] is not None]
        correct_predictions = [order for order in settled_orders
                               if order["result"] in SUPPORTED_BINARY_RESULTS and order["side"] == order["result"]]
        prediction_orders = [order for order in settled_orders
                             if order["result"] in SUPPORTED_BINARY_RESULTS and order["side"] in SUPPORTED_BINARY_RESULTS]
        settled_count = len(settled_orders)
        analytics_realized = round(sum(float(order["realized_pnl"]) for order in pnl_orders), 2)
        analytics = {
            "win_rate": round(sum(order["outcome"] == "won" for order in settled_orders) / settled_count, 4)
                if settled_count else None,
            "settled_positions": settled_count,
            "open_positions": sum(order["outcome"] in {"pending", "review_required"} for order in orders),
            "average_realized_pnl": round(analytics_realized / len(pnl_orders), 2) if pnl_orders else None,
            "total_realized_pnl": realized,
            "prediction_accuracy": round(len(correct_predictions) / len(prediction_orders), 4)
                if prediction_orders else None,
        }
        cash = round(self.starting_balance - spent + paid_out, 2)

        # Mark open positions to the latest side-specific bid. This is a paper
        # estimate: it does not simulate a fill, slippage, or a closing fee.
        cents = Decimal("0.01")
        estimated_position_value = Decimal("0.00")
        estimated_unrealized_pnl = Decimal("0.00")
        missing_valuations = 0
        for order in orders:
            order["estimated_mark_price"] = None
            order["estimated_current_value"] = None
            order["estimated_unrealized_pnl"] = None
            if order["outcome"] == "pending":
                quote = (current_quotes or {}).get(order["ticker"], {})
                raw_mark = quote.get(f"{order['side']}_bid") if isinstance(quote, dict) else None
                try:
                    mark = Decimal(str(raw_mark))
                    if not mark.is_finite() or not Decimal("0") <= mark <= Decimal("1"):
                        raise InvalidOperation
                    value = (mark * int(order["quantity"])).quantize(cents, rounding=ROUND_HALF_UP)
                    pnl = (value - Decimal(str(order["max_loss"]))).quantize(cents, rounding=ROUND_HALF_UP)
                except (InvalidOperation, TypeError, ValueError):
                    order["valuation_status"] = "unavailable"
                    missing_valuations += 1
                    continue
                order["estimated_mark_price"] = float(mark)
                order["estimated_current_value"] = float(value)
                order["estimated_unrealized_pnl"] = float(pnl)
                order["valuation_status"] = "estimated"
                estimated_position_value += value
                estimated_unrealized_pnl += pnl
            elif order["outcome"] == "review_required":
                order["valuation_status"] = "review_required"
                missing_valuations += 1
            else:
                order["valuation_status"] = "settled"

        valuation_complete = missing_valuations == 0
        return {
            "starting_balance": self.starting_balance,
            "cash": cash,
            "realized_pnl": realized,
            "estimated_open_position_value": float(estimated_position_value) if valuation_complete else None,
            "estimated_unrealized_pnl": float(estimated_unrealized_pnl) if valuation_complete else None,
            "estimated_total_value": round(cash + float(estimated_position_value), 2) if valuation_complete else None,
            "valuation_complete": valuation_complete,
            "valuation_missing_count": missing_valuations,
            "pending_count": sum(order["outcome"] == "pending" for order in orders),
            "tracking_enabled": tracking_enabled,
            "analytics": analytics,
            "orders": orders,
        }


class OutcomeTracker:
    """Poll unresolved tickers with bounded frequency; never sends orders."""
    def __init__(self, portfolio: PaperPortfolio, provider, interval_seconds: int = 300):
        self.portfolio = portfolio
        self.provider = provider
        self.interval_seconds = max(60, int(interval_seconds))
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def refresh_once(self):
        # Mock prices are illustrations and must never be used to claim actual settlements.
        if getattr(self.provider, "source", None) != "kalshi_demo":
            return
        tickers = self.portfolio.pending_tickers()
        if not tickers:
            return

        # Providers with batch support fetch up to 50 tickers per GET. Keep the
        # single-market fallback for older/custom providers and test doubles.
        if callable(getattr(self.provider, "get_markets", None)):
            try:
                markets = self.provider.get_markets(tickers)
            except (ProviderError, ValueError) as exc:
                for ticker in tickers:
                    self.portfolio.record_tracking_error(ticker, str(exc))
                LOG.warning("paper_outcome_batch_check_failed", extra={"ticker_count": len(tickers), "reason": str(exc)})
                return
            for ticker in tickers:
                try:
                    market = markets.get(ticker) if isinstance(markets, dict) else None
                    if not isinstance(market, dict) or not isinstance(market.get("status"), str):
                        raise ProviderError("Demo API did not return the requested market")
                    self.portfolio.update_market(ticker, market["status"], market.get("result"))
                except (ProviderError, ValueError) as exc:
                    self.portfolio.record_tracking_error(ticker, str(exc))
                    LOG.warning("paper_outcome_check_failed", extra={"ticker": ticker, "reason": str(exc)})
            return

        for ticker in tickers:
            try:
                market = self.provider.get_market(ticker)
                self.portfolio.update_market(ticker, market["status"], market.get("result"))
            except (ProviderError, ValueError) as exc:
                self.portfolio.record_tracking_error(ticker, str(exc))
                LOG.warning("paper_outcome_check_failed", extra={"ticker": ticker, "reason": str(exc)})

    def wake(self):
        self._wake.set()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="paper-outcome-tracker", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self):
        while not self._stop.is_set():
            self.refresh_once()
            self._wake.wait(self.interval_seconds)
            self._wake.clear()

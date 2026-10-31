"""Market data providers. The live adapter is hard-pinned to Kalshi demo."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

DEMO_BASE = "https://external-api.demo.kalshi.co/trade-api/v2"
SUPPORTED_DEMO_BASES = {DEMO_BASE, "https://demo-api.kalshi.co/trade-api/v2"}
MARKET_TICKER_PATTERN = re.compile(r"[A-Z0-9._-]{1,100}")


def valid_market_ticker(ticker: object) -> bool:
    """Accept punctuation used in Kalshi market tickers while rejecting path syntax."""
    return isinstance(ticker, str) and MARKET_TICKER_PATTERN.fullmatch(ticker) is not None


class ProviderError(RuntimeError):
    pass


@dataclass
class Market:
    ticker: str
    title: str
    category: str
    status: str
    close_time: str
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    volume: float | None
    open_interest: float | None
    rules: str
    source: str
    result: str | None = None


def ensure_demo_url(url: str) -> str:
    """Fail closed: do not permit production Kalshi or arbitrary hosts."""
    parsed = urlsplit(url)
    if url not in SUPPORTED_DEMO_BASES:
        raise ValueError("KALSHI_BASE_URL must be an official Kalshi demo Trade API URL")
    return DEMO_BASE


def _number(data: dict, *keys: str) -> float | None:
    for key in keys:
        value = data.get(key)
        if value is not None:
            try:
                number = float(value)
                if number == number and abs(number) != float("inf"):
                    return number
            except (TypeError, ValueError):
                return None
    return None


def normalize(raw: dict, source: str) -> Market:
    if not isinstance(raw, dict) or not isinstance(raw.get("ticker"), str):
        raise ProviderError("Invalid market response")
    title = raw.get("title") or raw.get("subtitle") or raw.get("yes_sub_title") or raw["ticker"]
    return Market(
        ticker=raw["ticker"][:100], title=str(title)[:500],
        category=str(raw.get("category") or "Other")[:80],
        status=str(raw.get("status") or "unknown")[:40],
        close_time=str(raw.get("close_time") or raw.get("expected_expiration_time") or "")[:60],
        yes_bid=_number(raw, "yes_bid_dollars", "yes_bid"),
        yes_ask=_number(raw, "yes_ask_dollars", "yes_ask"),
        no_bid=_number(raw, "no_bid_dollars", "no_bid"),
        no_ask=_number(raw, "no_ask_dollars", "no_ask"),
        volume=_number(raw, "volume_fp", "volume"),
        open_interest=_number(raw, "open_interest_fp", "open_interest"),
        rules=str(raw.get("rules_primary") or "Market rules were not provided by this data source.")[:5000],
        source=source,
        result=str(raw.get("result") or "").lower() or None,
    )


class MockProvider:
    source = "mock"

    def list_markets(self, status: str = "open") -> list[Market]:
        # Fixed timestamps keep mock fixtures reproducible across refreshes.
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        samples = [
            ("DEMO-INFLATION-26", "Will monthly inflation exceed 0.3%?", "Economics", .34, .37, 1240, "Resolves using the official CPI release named in the contract."),
            ("DEMO-WEATHER-NYC", "Will NYC record more than 1 inch of snow this week?", "Weather", .21, .25, 860, "Resolves using the listed official weather station and measurement period."),
            ("DEMO-SPORTS-FINAL", "Will the home team win the next demonstration game?", "Sports", .48, .52, 2300, "Resolves using the official final result; illustrative mock market."),
            ("DEMO-RATES-26", "Will the policy rate be lower by year end?", "Economics", .40, .44, 510, "Resolves against the stated official policy-rate source."),
        ]
        return [Market(t, title, cat, "open", (now + timedelta(days=3+i)).isoformat(), bid, ask, round(1-ask, 2), round(1-bid, 2), vol, vol*.4, rules, self.source)
                for i, (t, title, cat, bid, ask, vol, rules) in enumerate(samples)
                if status == "all" or status == "open"]

    def get_market(self, ticker: str) -> dict:
        market = next((m for m in self.list_markets("all") if m.ticker == ticker), None)
        if market is None:
            raise ProviderError("Market is not present in the mock catalog")
        return {"ticker": market.ticker, "status": market.status, "result": market.result,
                "yes_bid": market.yes_bid, "no_bid": market.no_bid}

    def get_markets(self, tickers: list[str]) -> dict[str, dict]:
        result = {}
        for ticker in dict.fromkeys(tickers):
            try:
                result[ticker] = self.get_market(ticker)
            except ProviderError:
                continue
        return result


class KalshiDemoProvider:
    source = "kalshi_demo"

    def __init__(self, base_url: str = DEMO_BASE):
        self.base_url = ensure_demo_url(base_url)

    def list_markets(self, status: str = "open") -> list[Market]:
        return [market for page in self.iter_market_pages(status) for market in page]

    def iter_market_pages(self, status: str = "open"):
        """Yield normalized pages while following cursors, for incremental UI loading."""
        if status not in {"open", "closed", "settled", "unopened", "all"}:
            raise ValueError("Unsupported market status")
        cursor = None
        seen_cursors = set()
        while True:
            # Use the endpoint's maximum page size to keep large demo catalogs
            # responsive while still following every returned cursor.
            query = {"limit": "1000", "mve_filter": "exclude"}
            if status != "all":
                query["status"] = status
            if cursor:
                query["cursor"] = cursor
            url = f"{self.base_url}/markets?{urlencode(query)}"
            request = Request(url, headers={"Accept": "application/json", "User-Agent": "kalshi-learning-lab/1.0"})
            try:
                with urlopen(request, timeout=8) as response:
                    data = json.loads(response.read(16_000_000))
            except HTTPError as exc:
                # No retries: read-only requests fail visibly; error body is not logged.
                raise ProviderError(f"Demo API returned HTTP {exc.code}") from None
            except (URLError, TimeoutError, json.JSONDecodeError, OSError):
                raise ProviderError("Could not retrieve demo market data") from None
            rows = data.get("markets") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                raise ProviderError("Demo API returned an invalid markets payload")
            page = []
            for row in rows:
                try:
                    page.append(normalize(row, self.source))
                except ProviderError:
                    continue
            yield page

            next_cursor = data.get("cursor")
            if next_cursor in (None, ""):
                break
            if not isinstance(next_cursor, str):
                raise ProviderError("Demo API returned an invalid markets cursor")
            if next_cursor in seen_cursors:
                raise ProviderError("Demo API returned a repeated markets cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def get_market(self, ticker: str) -> dict:
        """Read one public demo market using the batch lookup endpoint."""
        if not valid_market_ticker(ticker):
            raise ValueError("Invalid market ticker")
        market = self.get_markets([ticker]).get(ticker)
        if market is None:
            raise ProviderError("Demo API did not return the requested market")
        return market

    def get_markets(self, tickers: list[str]) -> dict[str, dict]:
        """Fetch demo market status/results in batches rather than one request per ticker."""
        requested = list(dict.fromkeys(tickers))
        if any(not valid_market_ticker(ticker) for ticker in requested):
            raise ValueError("Invalid market ticker")
        found = {}
        for offset in range(0, len(requested), 50):
            batch = requested[offset:offset + 50]
            query = {"limit": "1000", "mve_filter": "exclude", "tickers": ",".join(batch)}
            url = f"{self.base_url}/markets?{urlencode(query)}"
            request = Request(url, headers={"Accept": "application/json", "User-Agent": "kalshi-learning-lab/1.0"})
            try:
                with urlopen(request, timeout=8) as response:
                    data = json.loads(response.read(2_000_000))
            except HTTPError as exc:
                raise ProviderError(f"Demo API returned HTTP {exc.code}") from None
            except (URLError, TimeoutError, json.JSONDecodeError, OSError):
                raise ProviderError("Could not retrieve demo market settlement data") from None
            rows = data.get("markets") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                raise ProviderError("Demo API returned an invalid markets payload")
            requested_in_batch = set(batch)
            for row in rows:
                try:
                    normalized = normalize(row, self.source)
                except ProviderError:
                    continue
                if normalized.ticker not in requested_in_batch or not isinstance(row.get("status"), str):
                    continue
                raw_result = row.get("result")
                found[normalized.ticker] = {
                    "ticker": normalized.ticker,
                    "status": normalized.status.lower(),
                    "result": raw_result.lower() if isinstance(raw_result, str) else None,
                    "yes_bid": normalized.yes_bid,
                    "no_bid": normalized.no_bid,
                }
        return found


class CachedProvider:
    """Short-lived cache decorator for provider market reads."""

    def __init__(self, provider, ttl_seconds: float = 30, clock=time.monotonic):
        self.provider = provider
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self._clock = clock
        self._lock = threading.RLock()
        self._lists: dict[str, tuple[float, list[Market]]] = {}
        self._markets: dict[str, tuple[float, dict]] = {}
        self._list_jobs: dict[str, dict] = {}

    @property
    def source(self):
        return self.provider.source

    def __getattr__(self, name):
        # Keep provider-specific capabilities available through the decorator.
        return getattr(self.provider, name)

    def _fresh(self, entry):
        return entry is not None and self._clock() < entry[0]

    def _expiry(self):
        return self._clock() + self.ttl_seconds

    def list_markets(self, status: str = "open") -> list[Market]:
        key = status
        with self._lock:
            entry = self._lists.get(key)
            if self._fresh(entry):
                return deepcopy(entry[1])
        markets = self.provider.list_markets(status)
        with self._lock:
            self._lists[key] = (self._expiry(), deepcopy(markets))
        return markets

    def market_list_snapshot(self, status: str = "open") -> tuple[list[Market], bool, str | None]:
        """Return cached/partial data immediately and refresh large lists in background."""
        if self.source == "kalshi_demo" and status not in {"open", "closed", "settled", "unopened", "all"}:
            raise ValueError("Unsupported market status")
        if self.ttl_seconds == 0:
            return self.list_markets(status), False, None
        with self._lock:
            entry = self._lists.get(status)
            job = self._list_jobs.get(status)
            cached = deepcopy(entry[1]) if entry else []
            if job and job.get("loading"):
                return cached[:1000], True, None
            if job and job.get("error"):
                error = job.pop("error")
                self._list_jobs.pop(status, None)
                return cached, False, error
            if self._fresh(entry):
                return cached, False, None
            # Start a clean generation so progress offsets never mix stale rows
            # with a newly paginated catalog.
            self._lists.pop(status, None)
            self._list_jobs[status] = {"loading": True}
            worker = threading.Thread(target=self._refresh_market_list, args=(status,),
                                      name=f"market-list-refresh-{status}", daemon=True)
            worker.start()
            return [], True, None

    def market_list_delta(self, status: str, offset: int) -> tuple[list[Market], int, bool, str | None]:
        """Return only newly loaded rows plus progress, avoiding repeated full-list transfers."""
        _, _, snapshot_error = self.market_list_snapshot(status)  # starts or reuses the background refresh
        with self._lock:
            entry = self._lists.get(status)
            if snapshot_error:
                return [], len(entry[1]) if entry else 0, False, snapshot_error
            job = self._list_jobs.get(status)
            if job and job.get("error"):
                error = job.pop("error")
                self._list_jobs.pop(status, None)
                return [], len(entry[1]) if entry else 0, False, error
            rows = entry[1] if entry else []
            page = rows[offset:offset + 1000]
            loading = bool(job and job.get("loading")) or offset + len(page) < len(rows)
            return deepcopy(page), len(rows), loading, None

    def _refresh_market_list(self, status: str) -> None:
        try:
            page_reader = getattr(self.provider, "iter_market_pages", None)
            if callable(page_reader):
                markets = []
                for page in page_reader(status):
                    markets.extend(page)
                    with self._lock:
                        self._lists[status] = (self._expiry(), deepcopy(markets))
            else:
                markets = self.provider.list_markets(status)
            with self._lock:
                self._lists[status] = (self._expiry(), deepcopy(markets))
                self._list_jobs.pop(status, None)
        except Exception as exc:
            error = str(exc) if isinstance(exc, (ProviderError, ValueError)) else "Could not retrieve demo market data"
            with self._lock:
                self._list_jobs[status] = {"loading": False, "error": error}

    def get_markets(self, tickers: list[str]) -> dict[str, dict]:
        unique = list(dict.fromkeys(tickers))
        with self._lock:
            cached = {ticker: deepcopy(self._markets[ticker][1])
                      for ticker in unique
                      if self._fresh(self._markets.get(ticker))}
        missing = [ticker for ticker in unique if ticker not in cached]
        if missing:
            batch_method = getattr(self.provider, "get_markets", None)
            if callable(batch_method):
                fresh = batch_method(missing)
            else:
                fresh = {}
                for ticker in missing:
                    try:
                        fresh[ticker] = self.provider.get_market(ticker)
                    except ProviderError:
                        continue
            if not isinstance(fresh, dict):
                raise ProviderError("Provider returned an invalid market lookup")
            expiry = self._expiry()
            with self._lock:
                for ticker, market in fresh.items():
                    if ticker in missing and isinstance(market, dict):
                        self._markets[ticker] = (expiry, deepcopy(market))
                        cached[ticker] = deepcopy(market)
        return cached

    def get_market(self, ticker: str) -> dict:
        markets = self.get_markets([ticker])
        if ticker in markets:
            return markets[ticker]
        # Preserve each provider's established missing-market behavior.
        return self.provider.get_market(ticker)


def make_provider(environ: dict[str, str] | None = None):
    env = environ if environ is not None else os.environ
    mode = env.get("KALSHI_PROVIDER", "mock").strip().lower()
    if mode == "mock":
        provider = MockProvider()
    elif mode == "kalshi_demo":
        provider = KalshiDemoProvider(env.get("KALSHI_BASE_URL", DEMO_BASE))
    else:
        raise ValueError("KALSHI_PROVIDER must be mock or kalshi_demo")
    ttl = float(env.get("KALSHI_MARKET_CACHE_TTL_SECONDS", "30"))
    return CachedProvider(provider, ttl_seconds=ttl)


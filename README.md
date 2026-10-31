# Dressed To Kill — Kalshi Learning Lab

Project version name: **Dressed To Kill**. Future project versions will use hard rock album names.

A local-first learning app for browsing Kalshi demo markets, practicing with a paper portfolio, and tracking demo settlement outcomes. Paper orders are handled by this app and are never sent to Kalshi.

## What it does

- Lists and filters sample markets in **mock** mode, with no internet access or credentials required.
- In `kalshi_demo` mode, reads public market data and settlement status from Kalshi's **demo** Trade API. No authenticated account endpoints or real/demo order endpoints are called.
- Shows question, contract ticker, status, close time, yes bid/ask, volume, open interest, and settlement rules when returned by the data source.
- Lets you submit a local paper order after a confirmation screen that shows side, quantity, price, settlement terms, estimated fee, and total maximum amount at risk.
- Keeps a $1,000 simulated cash balance and paper orders in SQLite. Docker Compose stores this database in a project-specific named volume, so it survives container rebuilds and restarts.
- Shows estimated open-position value, unrealized P&L, and total portfolio value using the latest available side-specific bid. These estimates are separate from realized P&L and are marked incomplete when an open or review position has no usable quote.
- Shows portfolio analytics from saved orders: position win rate, settled and open counts, average and total realized P&L, and prediction accuracy. Review-required positions remain open and are excluded from settled-result rates.
- Provides CSV downloads for all positions, finalized settlements, and realized P&L history from `GET /api/portfolio/export?type=positions|settlements|realized-pnl`. Rows stream from SQLite in bounded batches and use UTF-8 CSV with CRLF row endings.
- In `kalshi_demo` mode, checks unresolved paper positions about every five minutes and records settled Yes/No outcomes and realized paper P&L. Unknown settlement results are flagged for review rather than guessed.

Kalshi documents market discovery as `GET /markets`, which supports cursor pagination for the market list and a comma-separated ticker filter for grouped lookups. Market-list pages request up to 1,000 records to reduce sequential API round trips while following every returned cursor. The app loads pages in a background worker; the browser retrieves only new rows from `/api/markets/progress` while showing a loading count, then searches and filters the complete catalog. Official docs: [Get Markets](https://docs.kalshi.com/api-reference/market/get-markets), [Get Market](https://docs.kalshi.com/api-reference/market/get-market).

## Paper fee estimates

Checked October 31, 2026 against Kalshi's [official fee schedule (PDF)](https://kalshi.com/docs/kalshi-fee-schedule.pdf), [fee schedule page](https://kalshi.com/regulatory/fee-schedule), and [Fees help article](https://help.kalshi.com/en/articles/13823805-fees). Kalshi's published general event-contract formulas are `ceil_to_cent(0.07 × contracts × P × (1-P))` for taker fees and `ceil_to_cent(0.0175 × contracts × P × (1-P))` for maker fees, where `P` is the contract price in USD. Each formula rounds up to the next cent. Kalshi says general taker fees apply to immediately matched orders; maker fees can apply to resting orders that later fill, while some resting orders are fee-free. The applicable maker/taker execution cannot be known in this paper simulator, so the user chooses which formula to estimate and the UI labels it as an estimate.

The fee schedule lists product-specific rates, including INX and NASDAQ100, and notes that individual markets can have distinct schedules. Because public market data used here does not reliably expose the fee schedule for each ticker, those known product families are shown as fee unavailable, rather than charged at the general rate. Other markets use the general estimate with a clear market-specific schedule caveat. The app uses Python `Decimal` and ceiling-to-cent rounding. Fee and order costs are USD. Fees are stored per order; legacy rows are migrated additively and retain their old maximum cost with fee marked unavailable. The simulator charges the estimated opening fee only; it does not model actual fills, fee rebates, closing trades, partial executions, other product schedules, or account-specific adjustments. Settlement fees are not added because Kalshi's published schedule says there is no settlement fee.

## Architecture

The app uses Python 3.12's standard library for a small HTTP server, SQLite persistence, and provider layer, plus plain HTML/CSS/JavaScript. `app/provider.py` contains the deterministic mock and read-only demo adapters; `app/portfolio.py` persists paper orders and applies resolved outcomes; `app/server.py` serves the UI and validates paper-order inputs. No third-party runtime package is needed.

Order creation uses a SQLite `BEGIN IMMEDIATE` transaction around the cash check and insert. The browser sends a unique `client_order_id`; a partial unique index makes retries idempotent, returning the original order instead of reserving cash twice. Reusing a key for a different ticker, side, quantity, or price is rejected.

`GET /healthz` returns a small JSON readiness response with application status, SQLite connectivity, and the configured provider name. It does not return database paths, exception details, or credentials. The existing `GET /api/health` endpoint remains available.

The demo adapter allows only Kalshi's documented demo URLs and defaults to the recommended `https://external-api.demo.kalshi.co/trade-api/v2`; production URLs and other hosts are rejected. Public market data is requested server-side with a timeout. Failures are recorded as short status notes and retried on the next polling interval. The app does not sign requests, use API keys, inspect an account, submit Kalshi orders, or include automated trading logic. Kalshi documents the demo website under `demo.kalshi.co` and the supported demo REST hosts in [API Environments and Endpoints](https://docs.kalshi.com/getting_started/api_environments).

Settlement and portfolio-valuation reads use Kalshi's `GET /markets` ticker filter, grouped into batches of up to 50 tickers. This reduces one-request-per-position polling to at most one request per 50 positions without changing the five-minute settlement check interval or the terminal status/result rules. The [Get Markets API](https://docs.kalshi.com/api-reference/market/get-markets) documents the comma-separated `tickers` filter.

Provider reads are wrapped in a short-lived in-memory cache shared by the app's market, valuation, and settlement paths. Market lists and per-ticker quote/status/result records expire after 30 seconds by default; set `KALSHI_MARKET_CACHE_TTL_SECONDS` to adjust the TTL or `0` to disable caching. Cache entries are isolated by market-list status and ticker, returned values are copied so callers cannot mutate cached data, and expired/missing entries are retrieved from the configured provider. The cache is process-local and does not change API response shapes or the database schema.

## Run locally

```sh
python3 -m app.server
```

Run that command from this project directory. Open <http://127.0.0.1:8765>. `KALSHI_PROVIDER=mock` works offline but cannot report actual settlements. Python does not read `.env` files automatically; set these environment variables in the shell to browse demo markets and track outcomes:

```sh
KALSHI_PROVIDER=kalshi_demo KALSHI_BASE_URL=https://external-api.demo.kalshi.co/trade-api/v2 python3 -m app.server
```

No API credentials are needed for the public demo market reads. The local database is written to `data/paper_portfolio.sqlite3` by default; `data/` is git-ignored.

## Run with Docker Compose

Docker must be running before starting the service.

```sh
docker compose up --build
```

Run that command from this project directory. Compose defaults to `kalshi_demo`, so demo markets and outcome checks are enabled without credentials. If an existing `.env` sets `KALSHI_PROVIDER=mock`, change it to `kalshi_demo`; `.env` values override Compose defaults. Visit <http://127.0.0.1:8766>. The dedicated `kalshiv2-learning-lab` container publishes on localhost port 8766 and stores SQLite data in the separate `kalshiv2_paper_data` volume. Stop with `Ctrl-C`; use `docker compose down` to remove this project's container. The named volume retains SQLite history across container rebuilds. Port 8765 remains the app's internal container port and is not published on the host. The app container does not mount the Docker socket.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Run the test command from this project directory.

Tests cover provider selection, demo-host enforcement, payload validation, batch market lookups and settlement polling, health endpoint readiness and error handling, documented fee formulas and cent rounding, fee-unavailable products, concurrent cash reservation and idempotent order retries, additive SQLite upgrades from pre-settlement and pre-fee schemas, historical order and balance preservation, repeated migration safety, restart/recovery of persisted portfolios and settlement tracking, analytics derived from historical outcomes, binary outcome accounting, and the mock-mode tracking guard. Migration tests deny SQLite delete/drop operations while the upgrade runs. The demo network call itself is not exercised by the test suite.

## Limitations and safety

- The paper portfolio is local and is not a Kalshi demo account or an actual fill. It records the entered price as the simulated entry price.
- Mark-to-market values are estimates using the latest Yes bid for Yes positions and No bid for No positions. Unrealized P&L subtracts the recorded maximum amount at risk, including any estimated opening fee. It does not model closing fees, slippage, executable size, or fills; missing quotes make aggregate value estimates unavailable.
- Kalshi's REST market lifecycle statuses are `initialized`, `active`, `inactive`, `closed`, `determined`, `disputed`, `amended`, and `finalized`. The first seven remain unresolved for paper accounting; only `finalized` is terminal in the current REST API. `settled` is a list-filter value, but is also accepted as a legacy terminal status for compatibility. Kalshi documents `yes`, `no`, and `scalar` as possible results after determination. The app pays paper positions only for finalized `yes`/`no`; missing or unsupported finalized results (including `scalar`) become `review_required` with no payout or P&L applied. Mock data never settles positions. See [Market Lifecycle](https://docs.kalshi.com/getting_started/market_lifecycle).
- The outcome checker runs only while the app is running in demo mode. After downtime it checks unresolved positions at startup. Demo API/network failures delay tracking until a later retry.
- Fee estimates follow the linked schedule checked date, but the real fee depends on actual execution type and the market's applicable schedule; see Paper fee estimates above. Slippage, partial fills, closing trades, and unrealized mark-to-market P&L are not modeled.
- The first page of up to 100 markets is read; cursor pagination and category metadata may be incomplete. Data availability depends on Kalshi's demo environment.
- The practice order price is manually adjustable. The app checks simulated cash including the estimated fee but does not model a fill book or execution uncertainty.
- Prices are market signals, not objective probabilities. Contract rules, liquidity, spread, stale data, fees, and full loss of the amount paid matter. This is educational software, not financial advice and does not promise revenue.
- Do not put credentials in this app. It has no credentialed API integration.
- `docker compose down` preserves paper history. `docker compose down -v` removes the project volume and permanently deletes saved paper orders; use it only when intentionally resetting the practice account.

## GitHub

The intended project repository is `https://github.com/WSWATERMAN/Dressed-To-Kill` (private). The project version is **Dressed To Kill**; future versions will use hard rock album names. `.env`, local SQLite data, and Python caches are excluded from Git.


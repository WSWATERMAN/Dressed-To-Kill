# Dressed To Kill — Kalshi Learning Lab Developer Handoff

## Project summary

**Project version name: Dressed To Kill.** Future project versions will use hard rock album names.

Local-first educational app for inspecting Kalshi demo event markets, recording simulated paper positions, estimating transaction fees, and tracking public demo settlement outcomes. It is not a revenue tool and does not place exchange orders.

Intended project repository: `https://github.com/WSWATERMAN/Dressed-To-Kill` (private). The local branch is `main`; review `git status` before committing or pushing. `.env`, local SQLite data, and Python caches are excluded from Git.

## Current capabilities

- Mock market catalog for offline use, or server-side, unauthenticated, read-only Kalshi demo API access.
- Searchable market list with status filtering; market detail shows rules, quotes, close time, volume, and open interest when supplied.
- Locally simulated Yes/No buy orders with confirmation, SQLite persistence, simulated cash control, settlement tracking, and realized net P&L.
- Order inserts use `BEGIN IMMEDIATE` and browser submissions carry a unique `client_order_id` for idempotent retry handling.
- Estimated mark-to-market value and unrealized P&L for open positions, using the latest side-specific bid from mock or demo market data; realized P&L remains separate.
- Portfolio analytics calculated from saved order history: win rate, settled/open position counts, average/total realized P&L, and prediction accuracy. Review-required outcomes count as open and are excluded from accuracy and win-rate denominators.
- CSV export is available at `/api/portfolio/export?type=positions`, `type=settlements`, or `type=realized-pnl`; it streams stored records from SQLite in batches and does not add schema or API changes to existing routes.
- Estimated opening fees using the schedule rate selected in the UI: general taker or maker formula. Known INX/NASDAQ100 ticker families are marked fee unavailable.
- Demo outcome polling every five minutes by default. `finalized` plus an explicit `yes`/`no` result finalizes the paper order. Missing or unsupported finalized results (including `scalar`) require review without applying payout or P&L. Known unresolved REST statuses are `initialized`, `active`, `inactive`, `closed`, `determined`, `disputed`, and `amended`; `settled` remains accepted as a legacy terminal alias.
- Market list and per-ticker quote/status/result responses use a process-local in-memory cache with a 30-second default TTL (`KALSHI_MARKET_CACHE_TTL_SECONDS`); setting it to `0` disables caching. This cache is volatile and does not add database state.

## Architecture

- `app/server.py`: Python standard-library HTTP server, REST routes including `/healthz`, input validation, startup, and tracker lifecycle.
- `app/provider.py`: deterministic mock provider and allowlisted Kalshi demo read-only provider. Demo URLs are exact-match allowlisted; production hosts are rejected.
- `app/fees.py`: Decimal fee estimation and cent-ceiling rounding for paper-only transaction fees.
- `app/portfolio.py`: SQLite schema/migration, simulated cash reservation, orders, outcomes, and polling worker.
- `app/static/`: plain HTML/CSS/JavaScript frontend; all API-provided text is escaped before display.
- `tests/test_provider.py`, `tests/test_fees.py`, `tests/test_migrations.py`, and `tests/test_recovery.py`: provider, validation, fee, persistence, additive migration compatibility, restart recovery, settlement, and safeguards tests.
- `compose.yaml`: dedicated `kalshiv2-learning-lab` container, published on localhost port `8766`, plus isolated SQLite volume `kalshiv2_paper_data`.
- `Dockerfile`: Python 3.12 slim container, stdlib only.

## Local development

From this directory:

```sh
python3 -m app.server
```

Mock data is the Python default. To access demo markets and settlement status:

```sh
KALSHI_PROVIDER=kalshi_demo KALSHI_BASE_URL=https://external-api.demo.kalshi.co/trade-api/v2 python3 -m app.server
```

Open <http://127.0.0.1:8765>. Local SQLite defaults to `data/paper_portfolio.sqlite3`.

## Docker

With Docker running:

```sh
docker compose up --build -d
```

Compose defaults to `kalshi_demo`, publishes only `127.0.0.1:8766`, and persists the database in the dedicated `kalshiv2_paper_data` volume. The container is named `kalshiv2-learning-lab`; the app listens on port `8765` inside the container. Stop with `docker compose down`. This preserves paper history. `docker compose down -v` deletes this project's volume and all stored paper data. Port 8080 is not used by this project; do not change or interfere with unrelated services.

## Configuration and data safety

- No API credentials are required or used. Do not add credential signing, authenticated account endpoints, live hosts, real order endpoints, or secrets to the app.
- `KALSHI_PROVIDER`: `mock` or `kalshi_demo`.
- `KALSHI_BASE_URL`: must be one of the exact official demo API URLs accepted by `app/provider.py`.
- `APP_HOST`, `APP_PORT`: Compose binds inside the container on `0.0.0.0:8765`; host publication is loopback-only at `127.0.0.1:8766`.
- `KALSHI_DB_PATH`: Compose path `/data/paper_portfolio.sqlite3`; local default under `data/`.
- Schema changes must be additive and tested against a database using the prior schema. Never delete/reset the persisted volume to apply a migration.

## Fee model caveats

The README records the official-source URLs and the fee schedule checked date (October 31, 2026). Current model formulas:

- Taker: `ceil_to_cent(0.07 × C × P × (1-P))`.
- Maker: `ceil_to_cent(0.0175 × C × P × (1-P))`.

`C` is contract quantity and `P` is dollar price per contract. These are estimates, not actual fees: the app has no fill simulation and cannot know the eventual liquidity role or every market-specific fee schedule. INX/NASDAQ100 are identified as special and return fee unavailable. The model saves the selected fee and fee status on each order, charges only an estimated opening fee, and includes it in cash and net P&L. It does not model closing fees, partial fills, rebates, or other special schedules. Recheck Kalshi's official fee schedule before changing the model.

## Verification

Most recent checks run for the fee-model change and price-input fix:

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q app tests
node --check app/static/app.js
docker compose config
git diff --check
```

The tests passed (18 tests). No container rebuild or live demo API call was performed during those checks. The app may need a local rebuild to load the latest source: `docker compose up --build -d`.

## Known limitations / suggested next work

- Market listing follows provider pagination cursors in a background worker, returns partial data and a loading flag while fetching up to 1,000 records per page, and serves incremental rows from `/api/markets/progress` before caching the full list; grouped settlement/valuation lookups are limited to 50 tickers per request.
- Demo endpoint availability and returned data can vary; errors delay outcome tracking until a later poll.
- Settlement checker runs only while the app runs; it batches unresolved ticker reads in groups of up to 50 and preserves the existing five-minute check interval.
- Paper orders use a user-entered limit-like price and are not fills; no order book, liquidity, execution, fee tier, or unrealized valuation is simulated.
- Consider adding API-level tests for order response/portfolio integration and browser tests for price/fee interactions. Avoid testing against live endpoints.

## Handoff checklist

1. Inspect `git status --short --branch`; the latest working-tree implementation has not necessarily been committed or pushed.
2. Run the checks above before making follow-on changes.
3. Read README and this document; consult official Kalshi docs before changing API or fee behavior.
4. Preserve the demo-only boundary, localhost port 8766, dedicated `kalshiv2_paper_data` volume, and existing paper portfolio records.

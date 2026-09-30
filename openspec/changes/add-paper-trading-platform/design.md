## Context

See `proposal.md` for motivation and scope. The repository holds only OpenSpec scaffolding; it is not yet a git repository. The development machine has Python 3.11.5, uv 0.12.9, Node 24 and npm 11. Docker and Postgres are not installed.

Decisions taken with the user before planning: Python API with a React UI; multi-user with accounts; private simulation only (no publishing or social features); manual paper trading only (no backtesting or automation).

External facts that constrain the design:

- ccxt covers crypto exchanges only. Yahoo Finance is not a ccxt exchange, so "ccxt as adapter" is realised as ccxt for crypto plus an in-house adapter interface with ccxt's shape for everything else.
- ccxt's public `load_markets()` returns each market's `maker` and `taker` rates, amount precision and minimum order size without an API key. OKX's published base-tier spot rates are 0.08% maker and 0.10% taker.
- Yahoo Finance has no official API. `yfinance` uses undocumented endpoints, quotes on some exchanges are delayed by up to 15 minutes, bid and ask are often missing, and the terms allow personal use only.
- TradingView has no market-data API for subscribers on any plan; its REST API is for broker integrations. Its charting library, Lightweight Charts, is open source (Apache 2.0).

## Goals / Non-Goals

**Goals:**

- One adapter interface for all price sources, so adding a source is a new adapter class plus a configuration entry.
- Exact money arithmetic: no binary floating point between a source's response and a stored balance.
- Fills that are correct under concurrency: an order fills once, and cash never goes negative.
- Runs locally with `uv` and `npm` alone, and moves to Postgres by changing one setting.

**Non-Goals:**

- Horizontal scaling. The first version runs as one backend process.
- Streaming prices over WebSockets. Polling is sufficient for manual trading.
- Order-book depth, partial fills and slippage modelling beyond the bid-ask spread.
- User-defined fee profiles. Profiles are built in; editing them comes later.
- Corporate actions. Splits and dividends do not adjust positions.

## Decisions

### 1. Layout: `backend/` and `frontend/` in one repository

```
backend/
  pyproject.toml
  app/
    main.py  config.py  db.py
    models/        SQLAlchemy models
    api/           routers: auth, assets, market, portfolios, orders, fees
    marketdata/    base.py  ccxt_adapter.py  yahoo_adapter.py  router.py  cache.py  calendars.py
    trading/       engine.py  fees.py  matcher.py  valuation.py
    seed/          assets.yaml  fee_profiles.yaml
  alembic/
  tests/
frontend/
  src/  api/  pages/  components/
```

FastAPI serves JSON under `/api` and, in production, the built SPA as static files, so there is one deployable. In development Vite proxies `/api` to the backend. TypeScript types are generated from the OpenAPI document with `openapi-typescript`.

Alternative considered: Next.js full-stack in TypeScript. Rejected by the user in favour of Python because `yfinance` is the most robust Yahoo client and Python leaves the door open to backtesting.

### 2. Market-data adapter interface modelled on ccxt

`marketdata/base.py` defines a `MarketDataSource` protocol:

| Method | Returns |
| --- | --- |
| `load_markets()` | instruments with symbol, quote currency, amount step, minimum amount, maker and taker rates where published |
| `fetch_ticker(symbol)` | `Quote(last, bid, ask, currency, observed_at, source)` |
| `fetch_ohlcv(symbol, timeframe, since, limit)` | list of bars |
| `search(query)` | matching instruments with a detected asset class |

- `CcxtSource(exchange_id)` is one generic class over `ccxt.async_support`, with ccxt's built-in rate limiter enabled. OKX, Kraken and Coinbase are instances of it, so adding a crypto exchange is configuration only.
- `YahooSource` wraps `yfinance`. Its calls are blocking, so they run in a thread pool, behind a semaphore that limits concurrency to 2.

Alternative considered: calling ccxt and yfinance directly from the trading code. Rejected because fallback, caching and testing with a fake source all need one seam.

### 3. Source fallback through a router with per-asset symbol mapping

Assets have a canonical identity and a table of per-source symbols, for example Bitcoin maps to `BTC/USDT` on OKX, `BTC/USD` on Kraken and `BTC-USD` on Yahoo. `marketdata/router.py` holds the chain per asset class from configuration:

| Asset class | Chain in the first version |
| --- | --- |
| crypto | okx → kraken → coinbase → yahoo |
| stocks, ETFs, commodities, forex | yahoo |

Resolution: the router picks the first source in the chain whose `load_markets()` lists the asset's symbol and stores it as the asset's resolved source. It is re-resolved daily and whenever the resolved source fails. A source that fails three times in a row is skipped for 60 seconds (circuit breaker), and requests fall through to the next source that lists the asset.

The non-crypto chain has a single entry because no second source without an API key is known to be reliable. The chain is configuration, so a keyed provider can be appended without code changes beyond its adapter (see Open Questions).

### 4. TradingView: charts yes, data no

Price and portfolio charts use Lightweight Charts fed from our own `/api` history endpoints. TradingView is not a data source. Unofficial TradingView scrapers are excluded because they breach its terms of use and break without notice.

### 5. Quote cache in process

`marketdata/cache.py` is a TTL cache keyed by asset: 5 seconds for crypto, 60 seconds for other classes, with one in-flight request per key so concurrent callers share a fetch. This is what enforces the load limits in the market-data spec.

Alternative considered: Redis. Rejected for the first version because there is one process and no Docker on the development machine.

### 6. Persistence: SQLAlchemy 2 (async) with Alembic; SQLite by default, Postgres by setting

`DATABASE_URL` defaults to `sqlite+aiosqlite:///./data/app.db` with WAL mode and a busy timeout. A Postgres URL works unchanged. Tables: `users`, `sessions`, `login_attempts`, `assets`, `asset_source_symbols`, `fee_profiles`, `portfolios`, `portfolio_assets`, `portfolio_fee_profiles`, `positions`, `orders`, `trades`, `value_snapshots`.

Alternative considered: Postgres only. Rejected because it cannot be started on the development machine today. SQLite handles the write volume of manual trading.

### 7. Money as `Decimal` end to end

Prices, quantities, rates and balances are `decimal.Decimal` in Python and are stored through a custom column type: `NUMERIC(38, 18)` on Postgres and text on SQLite, because SQLite's numeric affinity silently converts to binary floating point. Source values that arrive as floats are converted with `Decimal(str(value))`. The API serialises decimals as strings, and the UI formats them without arithmetic beyond display.

### 8. Authentication: server-side sessions in an HttpOnly cookie

Passwords are hashed with argon2id (`argon2-cffi`). Sign-in creates a row in `sessions` and sets an opaque random token in an `HttpOnly`, `SameSite=Lax` cookie, `Secure` outside development. State-changing requests must be JSON, which, with `SameSite=Lax`, blocks cross-site form posts. Every query for portfolio data filters by the session's user id through one shared dependency; a miss returns 404 so other users' ids are not confirmed.

Alternative considered: JWT bearer tokens. Rejected because they cannot be revoked on sign-out without the same server-side table, and storing them in the browser exposes them to scripts.

### 9. Trading engine: one fill function, used by both paths

`trading/engine.py` exposes `try_fill(order, quote)`, called synchronously when an order is placed and by the background matcher for open orders. Inside one database transaction it:

1. claims the order with `UPDATE orders SET status='filled' WHERE id=? AND status='open'` and stops if no row changed, which guarantees a single fill;
2. computes fill price, conversion rate and fee;
3. checks the cash balance (buy) or the holding (sell) and marks the order rejected if it fails;
4. updates cash and the position, inserts the trade and a value snapshot.

On SQLite the transaction starts with `BEGIN IMMEDIATE`; on Postgres the portfolio row is locked with `SELECT … FOR UPDATE`. Duplicate submissions are blocked by a unique constraint on `(portfolio_id, client_order_id)`, where the UI generates the id when the order form opens.

Fill prices follow the paper-trading and fee-models specs. Fills are all-or-nothing.

### 10. Background matcher inside the API process

`trading/matcher.py` is an asyncio task started in FastAPI's lifespan. Each tick it selects the distinct assets that have open orders and an open market, fetches their quotes through the router (every 5 seconds for crypto, every 60 seconds for others) and calls `try_fill` for each order whose condition is met. The same task writes the daily value snapshot for every portfolio shortly after 00:00 UTC.

Alternative considered: a separate worker process or Celery. Rejected as unnecessary for one process; the matcher has no state outside the database, so moving it out later is mechanical. The constraint is that exactly one backend process may run, which a startup advisory lock file enforces.

### 11. Trading hours from `exchange-calendars`

`marketdata/calendars.py` maps each asset's calendar code to a rule: `24/7` for crypto; an `exchange-calendars` code such as `XNYS`, `XNAS` or `XETR` for stocks and ETFs, derived from the exchange Yahoo reports; a fixed rule for forex (Sunday 22:00 UTC to Friday 22:00 UTC); the CME and ICE futures calendars for commodities.

### 12. Seed catalog as a reviewed data file

`seed/assets.yaml` lists ten assets per class with their per-source symbols. The seeder is idempotent, keyed on the canonical symbol. Proposed content, to be checked against current rankings when the file is written:

| Class | Ranking basis | Assets |
| --- | --- | --- |
| crypto | market capitalisation, stablecoins excluded | BTC, ETH, XRP, BNB, SOL, DOGE, ADA, TRX, LINK, AVAX |
| stocks | market capitalisation, US-listed | NVDA, MSFT, AAPL, GOOGL, AMZN, META, AVGO, TSLA, BRK-B, LLY |
| ETFs | assets under management | SPY, IVV, VOO, VTI, QQQ, VEA, VUG, IEFA, VTV, BND |
| commodities | front-month futures by traded volume | GC=F, SI=F, CL=F, BZ=F, NG=F, HG=F, PL=F, PA=F, ZC=F, ZW=F |
| forex | traded volume | EURUSD, USDJPY, GBPUSD, AUDUSD, USDCAD, USDCHF, NZDUSD, EURGBP, EURJPY, EURCHF |

BNB is not listed on OKX, so it exercises the fallback chain from day one. Commodities are simulated as unleveraged exposure to the front-month futures price: one unit is one unit of the quoted price, fractional quantities allowed, with no contract size, margin or expiry.

Quantity steps: from the exchange's amount precision for crypto; 1 for stocks and ETFs; 0.01 for commodities and forex.

### 13. Fee profiles as seeded data

`seed/fee_profiles.yaml` is loaded into `fee_profiles`. `trading/fees.py` implements the formula in the fee-models spec as a pure function, which makes it directly unit-testable. The maximum charge may be an absolute amount or a percentage of trade value.

| Profile | Classes | Charges (to be verified against the published schedule when seeded) | Default for |
| --- | --- | --- | --- |
| OKX spot, base tier | crypto | maker 0.08%, taker 0.10%; per-market rates from `load_markets()` take precedence | crypto |
| Interactive Brokers Fixed, US | stocks, ETFs | USD 0.005 per share, minimum USD 1.00, maximum 1% of trade value; assumed spread 0.02% | stocks, ETFs |
| Flat fee per order | stocks, ETFs | EUR 1.00 per order; assumed spread 0.02% | — |
| Interactive Brokers FX | forex | 0.002% of trade value, minimum USD 2.00; assumed spread 0.01% | forex |
| Spread only | commodities, forex | no commission; assumed spread 0.05% | commodities |
| Zero commission | all | no charges, no assumed spread | — |

Fees are charged in cash in the portfolio's base currency. Real crypto exchanges deduct the buy fee from the asset received; charging cash instead keeps position quantities equal to order quantities and changes the result by a negligible amount.

The commodity default is an approximation: no retail venue sells unleveraged, fractional exposure to a futures price, so there is no published schedule to copy. Spread-only pricing is how CFD brokers charge for the closest real product. The assumed-spread values are likewise estimates, used only when the source gives no bid and ask.

### 14. Currencies

Portfolio base currency is EUR or USD. Conversion rates come from the forex quotes already in the catalog (`EURUSD=X` on Yahoo), through the same cache. USDT-quoted prices are treated as USD one to one.

### 15. Frontend

React with TypeScript on Vite, React Router, TanStack Query for fetching and polling (quotes every 5 seconds for crypto and 30 seconds otherwise, only for visible assets), Lightweight Charts, and plain CSS with custom properties. No component library.

Pages: sign in and register; portfolio list; portfolio detail (holdings, tracked assets, value chart, performance and fees); asset browser by class with search; asset detail with price chart and order ticket; orders and trades with CSV download; fee profile settings per portfolio.

### 16. Testing

`pytest` with a `FakeSource` adapter that returns scripted quotes, so fills, fallback, staleness and market hours are tested deterministically and offline. Fee calculation and position accounting get table-driven unit tests taken from the spec scenarios. Adapter tests that hit OKX and Yahoo are marked `live` and excluded from the default run. `vitest` covers frontend formatting and the order ticket's validation.

## Risks / Trade-offs

- [Yahoo endpoints change or block requests without notice] → Yahoo is isolated behind one adapter; failures surface as stale or unavailable data, never as invented prices; `yfinance` is pinned and the `live` tests detect breakage.
- [Yahoo's terms allow personal use only, and the platform is multi-user] → Acceptable for private use among a few people. Before hosting it publicly, replace Yahoo with a licensed provider; the adapter interface and chain configuration are built for that swap.
- [Stock quotes delayed by up to 15 minutes fill orders at outdated prices] → Quotes are labelled delayed and the trade records the observation time, so the simulation is honest about what price it used.
- [Polling misses a price that touched a limit between two polls] → Accepted. The simulation is slightly conservative: an order may fill later than in reality, never at a price that was not observed.
- [Continuous front-month futures symbols jump when the contract rolls, creating profit or loss that no holder would have had] → Documented on commodity assets in the UI. Roll adjustment is left to a later change.
- [Stock splits make a held position's price drop while its quantity stays the same] → Known limitation of this version, stated in the UI; corporate actions are out of scope.
- [USDT deviates from USD] → Deviation is normally within a few basis points; the approximation is documented.
- [OKX is unreachable from some countries or networks] → The circuit breaker moves crypto pricing to Kraken or Coinbase automatically.
- [Two backend processes would double-evaluate orders] → The conditional status update makes double fills impossible regardless; the startup lock prevents the wasted work.
- [SQLite allows one writer at a time] → Fill transactions are short; WAL and a busy timeout absorb contention at this scale; Postgres is one setting away.
- [Built-in fee schedules go out of date] → Each profile stores its source and the date it was checked, shown in the UI; crypto rates are read live from the exchange.

## Migration Plan

Greenfield, so there is nothing to migrate. Initialise git, create the schema with the first Alembic migration, and run the idempotent seeders at startup. Rollback is deleting the database file.

## Open Questions

- Which keyed provider (for example Twelve Data, Finnhub or Polygon) becomes the second source for stocks, ETFs, commodities and forex. This adds an adapter and a chain entry and changes nothing else.
- Where the platform will be hosted, if at all. This decides when Postgres and a licensed price source become necessary.

## 1. Project setup

- [x] 1.1 Run `git init`, add a `.gitignore` covering Python, Node, `data/` and `.env`, and verify `git status` shows a clean tree after the first commit
- [x] 1.2 Create `backend/` with `uv init`, add fastapi, uvicorn, sqlalchemy, aiosqlite, alembic, pydantic-settings, argon2-cffi, ccxt, yfinance, exchange-calendars, pyyaml and dev dependencies pytest, pytest-asyncio, httpx, ruff; verify `uv sync` succeeds and `uv run python -c "import ccxt, yfinance"` exits 0
- [x] 1.3 Add `app/main.py` with a `/api/health` endpoint and `app/config.py` reading `DATABASE_URL` and the source chains from the environment; verify `uv run uvicorn app.main:app` answers `/api/health` with 200
- [x] 1.4 Scaffold `frontend/` with Vite (React, TypeScript), add react-router, @tanstack/react-query, lightweight-charts, vitest and openapi-typescript, and proxy `/api` to the backend; verify `npm run dev` shows a page that displays the health check result
- [x] 1.5 Add a root `README.md` with the two start commands and the environment settings; verify a fresh clone starts by following it

## 2. Database foundation

- [x] 2.1 Implement `app/db.py` with the async engine, session dependency, and SQLite WAL and busy-timeout pragmas; verify a test opens a session against a temporary database
- [x] 2.2 Implement the `Decimal` column type (text on SQLite, `NUMERIC(38,18)` on Postgres); verify a test round-trips `Decimal("0.1") + Decimal("0.2")` and an 18-decimal value exactly
- [x] 2.3 Configure Alembic for async use and autogeneration; verify `alembic upgrade head` and `alembic downgrade base` both succeed on an empty database
- [x] 2.4 Add the pytest fixtures for a per-test database and an HTTP test client; verify the health test passes through the client

## 3. User accounts

- [x] 3.1 Add `users`, `sessions` and `login_attempts` models and their migration; verify the migration applies and the email uniqueness test rejects a case-variant duplicate
- [x] 3.2 Implement registration with argon2id hashing and the 10-character minimum; verify tests for success, duplicate email and short password
- [x] 3.3 Implement sign-in, sign-out and the HttpOnly session cookie with 30-day idle expiry; verify tests for correct credentials, identical error for unknown email and wrong password, sign-out invalidation and an expired session
- [x] 3.4 Implement the 5-failures-in-15-minutes lockout; verify a test where the sixth attempt is refused with a retry time even with the correct password
- [x] 3.5 Implement the `current_user` dependency and the account endpoint; verify unauthenticated requests get 401 and the account response contains no password fields

## 4. Market data

- [x] 4.1 Define `Quote`, `Bar`, `Instrument` and the `MarketDataSource` protocol in `marketdata/base.py`, plus a scriptable `FakeSource` in the test package; verify a test drives `FakeSource` through every protocol method
- [x] 4.2 Implement `CcxtSource` over `ccxt.async_support` with rate limiting enabled, mapping markets to instruments (amount step, minimum amount, maker and taker rates) and tickers to quotes as `Decimal`; verify unit tests against recorded OKX responses and a `live`-marked test that fetches BTC/USDT from OKX
- [x] 4.3 Implement `YahooSource` over yfinance in a thread pool with a concurrency limit of 2, covering quotes, history and search with asset-class detection from Yahoo's quote type; verify unit tests against recorded responses and `live`-marked tests for AAPL, SPY, GC=F and EURUSD=X
- [x] 4.4 Implement the TTL quote cache with shared in-flight requests (5 s crypto, 60 s others); verify a test where 50 concurrent requests cause one source call
- [x] 4.5 Implement the source router with configured chains, per-asset symbol lookup, stored resolved source and the three-failure, 60-second circuit breaker; verify tests for not-listed fallback, failing-source fallback and the all-sources-failing error
- [x] 4.6 Implement `marketdata/calendars.py` for 24/7, exchange-calendars codes, the forex week and futures sessions, returning open status and next open; verify tests for a Saturday, a US exchange holiday, the forex weekend boundary and crypto
- [x] 4.7 Implement freshness classification (30 s crypto, 20 min others, stale when closed) and the delayed label; verify tests for each scenario in the market-data spec
- [x] 4.8 Implement currency conversion between EUR and USD from the forex quote, with USDT treated as USD; verify tests for USD to EUR, the inverse, and same-currency rate 1
- [x] 4.9 Add `/api/market` endpoints for quote, market status and daily and hourly history; verify API tests using `FakeSource`, including the empty-range case

## 5. Asset catalog

- [x] 5.1 Add `assets` and `asset_source_symbols` models and migration; verify the migration applies
- [x] 5.2 Write `seed/assets.yaml` with ten assets per class and their per-source symbols, checking each ranking against a current public listing and recording the date checked in the file; verify a test asserts five classes of exactly ten and no stablecoins
- [x] 5.3 Implement the idempotent seeder run at startup; verify a test that seeds twice and finds 50 assets with unchanged ids
- [x] 5.4 Implement the availability check that resolves each asset's source and metadata (quote currency, quantity step, minimum size, calendar) and marks unpriceable assets unavailable; verify tests where BNB resolves to a fallback source and an asset on no source is marked unavailable
- [x] 5.5 Add `/api/assets` endpoints for classes, assets by class, asset detail, search and add-from-search; verify API tests for adding a found asset, an empty search and refusal of an unsupported instrument type

## 6. Fee models

- [x] 6.1 Add `fee_profiles` and `portfolio_fee_profiles` models and migration; verify the migration applies
- [x] 6.2 Write `seed/fee_profiles.yaml` with the six profiles in design decision 13, confirming each rate against the venue's published schedule and recording the source URL and date checked; verify a seeder test finds a default profile for every asset class
- [x] 6.3 Implement the pure fee function in `trading/fees.py` (rate, per-unit, fixed, minimum, absolute or percentage maximum, maker or taker, currency conversion, half-up rounding); verify table-driven tests covering every Fee calculation scenario in the fee-models spec
- [x] 6.4 Implement fill-price derivation from a quote (ask or bid, else last adjusted by half the assumed spread); verify tests for both assumed-spread scenarios
- [x] 6.5 Implement crypto rate lookup from the pricing exchange's published schedule (seeded per exchange) with fallback to the built-in profile; verify tests for published rates and for the recorded fallback
- [x] 6.6 Add `/api/fee-profiles` listing and the per-portfolio profile selection endpoints; verify API tests for listing by class and for changing a profile

## 7. Portfolios

- [x] 7.1 Add `portfolios`, `portfolio_assets`, `positions` and `value_snapshots` models and migration; verify the migration applies
- [x] 7.2 Implement create, list, rename and delete with per-user name uniqueness, EUR or USD base currency, positive starting cash and default fee profiles; verify API tests for success, duplicate name, invalid cash and cascade on delete
- [x] 7.3 Enforce ownership through the shared dependency on every portfolio route; verify a test where a second user gets 404 on read, update, delete and trade
- [x] 7.4 Implement adding and removing tracked assets, refusing unavailable assets and removal while a position or open order exists; verify API tests for each case
- [x] 7.5 Implement `trading/valuation.py` for positions, market value, unrealised profit, total value, stale and unpriced flags and oldest-quote age; verify tests for mixed currencies, a closed market and a missing quote
- [x] 7.6 Implement performance figures (total return, realised and unrealised profit, fees paid, fees per asset class); verify the return-after-fees scenario and the fee totals scenario
- [x] 7.7 Implement the value-history endpoint and the creation-time snapshot; verify a new portfolio's series starts at its starting cash

## 8. Paper trading

- [x] 8.1 Add `orders` and `trades` models and migration, with the unique `(portfolio_id, client_order_id)` constraint and no update path for trades; verify the migration applies and a duplicate client id is rejected
- [x] 8.2 Implement order validation (type-specific prices, quantity step, minimum size, asset availability); verify tests for each refusal in the Place an order requirement
- [x] 8.3 Implement available cash and committed holdings from open orders and the funding checks; verify the insufficient-cash and selling-more-than-held scenarios
- [x] 8.4 Implement `try_fill` in `trading/engine.py` as one transaction (conditional status claim, price, conversion, fee, balance check, cash and average-cost position update, trade and snapshot insert); verify tests for the two-purchase average cost, partial sale realised profit, position close and the currency conversion scenario
- [x] 8.5 Implement market order placement with closed-market and stale-quote refusals including next open time; verify the four Market order execution scenarios
- [x] 8.6 Implement limit order handling, immediate taker fills and resting maker fills at the limit price; verify the three Limit order execution scenarios
- [x] 8.7 Implement stop order triggering on last price and fill at the triggering quote, with rejection when the cost exceeds cash; verify the stop-loss, gap and stop-buy-rejection scenarios
- [x] 8.8 Implement cancellation and the terminal-status rule; verify cancelling an open order releases its reservation and cancelling a filled order is refused
- [x] 8.9 Verify atomicity and single fill: a test that injects a failure inside `try_fill` leaves order, cash, position and trades unchanged, and a test that runs two concurrent `try_fill` calls for one order produces one trade
- [x] 8.10 Implement `trading/matcher.py` as a lifespan task evaluating open orders every 5 s for crypto and 60 s for others while markets are open, plus the daily snapshot and the single-process startup lock; verify a test with `FakeSource` where a resting order fills with no request in flight, and a test that the daily snapshot is written once per portfolio
- [x] 8.11 Add `/api/portfolios/{id}/orders` and `/trades` endpoints, the order preview endpoint and the CSV export; verify API tests for preview equal to fill, trade detail fields and a three-row CSV

## 9. Frontend

- [x] 9.1 Generate API types from the OpenAPI document and build the fetch client with session handling and redirect to sign-in on 401; verify `npm run typecheck` passes and an unauthenticated visit lands on sign-in
- [x] 9.2 Build the register and sign-in pages with error display; verify by registering, signing out and signing in again in the browser
- [x] 9.3 Build the portfolio list with create, rename and delete-with-confirmation; verify creating a EUR portfolio shows its starting cash as total value
- [x] 9.4 Build the asset browser with the five class tabs, live quotes with source and delayed or stale labels, unavailable marking, search and add-to-portfolio; verify each class shows ten assets and a coin that OKX does not list shows a non-OKX source
- [x] 9.5 Build the asset detail page with a Lightweight Charts price chart (daily and hourly) and market status; verify the chart renders a year of daily bars for one asset of each class
- [x] 9.6 Build the order ticket for market, limit and stop orders with client-side step and minimum validation, fee and cost preview, a fresh client order id per form, and refusal messages from the API; verify vitest tests for the validation and a manual market buy that matches its preview
- [x] 9.7 Build the portfolio detail page with cash and available cash, holdings with profit and loss, tracked assets, performance figures, fees by class and the value chart, polling only visible assets; verify the figures change after a trade without a page reload
- [x] 9.8 Build the orders and trades views with cancel for open orders, trade detail and CSV download; verify cancelling an open limit order restores available cash on screen
- [x] 9.9 Build the per-portfolio fee profile settings showing each profile's charges, source and date checked; verify switching the stock profile changes the next order preview
- [x] 9.10 Add the notices for delayed quotes, futures roll on commodities and unadjusted splits; verify they appear on the relevant asset pages

## 10. Integration and delivery

- [x] 10.1 Serve the built SPA from FastAPI with client-side route fallback; verify `npm run build` followed by starting only the backend serves the app and a deep link reloads correctly
- [x] 10.2 Run the full flow against live sources: register, create a portfolio, buy one asset from each class that is open, place a resting limit order, let the matcher fill or cancel it, and export trades; verify every trade shows a real source, observation time and non-zero fee where the profile charges one
- [x] 10.3 Run `uv run pytest`, `uv run ruff check`, `npm run typecheck` and `npm test`; verify all pass, and run the `live`-marked adapter tests once and record the result in the README
- [x] 10.4 Run `openspec validate add-paper-trading-platform --strict`; verify it reports no errors

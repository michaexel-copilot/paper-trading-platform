## Why

There is no way to try out a trading idea across crypto, stocks, ETFs, commodities and forex with realistic prices and costs without risking money or opening several broker accounts. This change builds a wikifolio-style paper-trading platform from scratch (the repository is empty apart from OpenSpec scaffolding) so a user can run simulated portfolios against real market prices and real fee schedules and see what a strategy would actually have returned after costs.

## What Changes

- Add a web platform: Python (FastAPI) API plus a React single-page UI.
- Add user accounts (register, log in, log out); every portfolio, order and trade belongs to exactly one user and is invisible to others.
- Add an asset catalog seeded with the top 10 assets in each of five asset classes: crypto, stocks, ETFs, commodities and forex. Users can also search for and add assets beyond the seed list.
- Add a market-data layer with a uniform adapter interface modelled on ccxt's unified API. First adapters: OKX through ccxt (crypto) and Yahoo Finance (stocks, ETFs, commodities, forex).
- Add source fallback: when the preferred source does not list an asset or is failing, the next source in that asset class's chain is used (for example a coin that OKX does not list resolves through another ccxt exchange or Yahoo). Every quote carries its source and timestamp.
- Add portfolios: a user creates a portfolio with a base currency and starting cash, adds assets to it, and sees positions, cash, valuation, profit and loss, fees paid and a value-over-time chart.
- Add simulated trading: market, limit and stop orders filled against real quotes, respecting trading hours, quote freshness, exchange minimums and available cash or holdings. Long-only, no leverage.
- Add fee models: every fill is charged according to a real published fee schedule (OKX maker/taker rates for crypto; a selectable broker fee profile for the other classes) and the fee is recorded on the trade.
- TradingView is used for charting only (the open-source Lightweight Charts library). It is not a price source: TradingView offers no market-data API to subscribers on any plan, and the unofficial scrapers breach its terms of use.

Out of scope for this change: public or shared portfolios, rankings and following; backtesting; automated or rule-based strategies; short selling, margin and derivatives; dividends and corporate actions; password reset and email verification; real-money trading of any kind.

## Capabilities

### New Capabilities
- `user-accounts`: registration, authentication, sessions, and isolation of each user's data.
- `asset-catalog`: asset classes, the seeded top-10 lists, asset metadata (currency, quantity step, trading hours) and asset search.
- `market-data`: quotes and price history from pluggable sources, source fallback, freshness and market-open status.
- `portfolios`: portfolio creation and management, tracked assets, positions, cash, valuation and performance history.
- `paper-trading`: order placement, validation, simulated execution, the order lifecycle and trade history.
- `fee-models`: fee schedules and profiles, fee calculation per fill, and fee reporting.

### Modified Capabilities

None. `openspec/specs/` is empty.

## Impact

- **Code**: greenfield. New `backend/` (Python 3.11+, FastAPI, SQLAlchemy, Alembic) and `frontend/` (React, TypeScript, Vite) trees; the directory becomes a git repository.
- **Dependencies**: `ccxt`, `yfinance`, `fastapi`, `sqlalchemy`, `alembic`, `argon2-cffi`, `exchange-calendars` on the backend; `react`, `@tanstack/react-query`, `react-router`, `lightweight-charts` on the frontend. Tooling: `uv`, `npm`, `pytest`, `vitest`.
- **External systems**: public, unauthenticated endpoints of OKX and fallback exchanges via ccxt; Yahoo Finance's unofficial endpoints via `yfinance`. No API keys are needed for the first version.
- **Data**: a new relational database. SQLite by default (neither Docker nor Postgres is installed on the development machine), Postgres through a connection setting for hosted use.
- **Known constraints**: Yahoo data is unofficial, delayed by up to 15 minutes on some exchanges, rate-limited and licensed for personal use, which matters if the platform is ever hosted for other people. The adapter interface keeps a licensed provider swappable in later.

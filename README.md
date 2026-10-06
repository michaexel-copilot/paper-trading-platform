# Paper Trading Platform

Simulate portfolios across crypto, stocks, ETFs, commodities and forex with real
market prices and real fee schedules. No real orders are ever placed.

- **Backend**: Python 3.11+, FastAPI, SQLAlchemy, SQLite by default (`backend/`)
- **Frontend**: React, TypeScript, Vite (`frontend/`)
- **Prices**: OKX, Kraken and Coinbase through [ccxt](https://github.com/ccxt/ccxt) for
  crypto; Yahoo Finance through `yfinance` for everything else. No API keys needed.

## Start it

You need [uv](https://docs.astral.sh/uv/) and Node.js 20 or newer.

```sh
# Terminal 1: the API on http://127.0.0.1:8000
cd backend
uv sync
uv run uvicorn app.main:app

# Terminal 2: the UI on http://localhost:5173
cd frontend
npm install
npm run dev
```

Open http://localhost:5173, create an account, create a portfolio and trade.

On its first start the backend creates `backend/data/app.db`, applies the database
migrations, seeds the top ten assets per class and the fee profiles, and then checks
each asset against the price sources. That check takes about half a minute.

### One process instead of two

```sh
cd frontend && npm run build
cd ../backend && uv run uvicorn app.main:app
```

The backend serves the built UI from `frontend/dist` at http://127.0.0.1:8000.

Run exactly one backend process. It evaluates open orders in the background, and a
lock file (`backend/data/backend.lock`) stops a second process from starting.
That means no `--workers` and no `--reload`.

## Deploy on a Linux server

On a headless Debian or Ubuntu machine, one command installs everything, builds the
platform and starts it as a systemd service on port 8000:

```sh
curl -fsSL https://raw.githubusercontent.com/michaexel-copilot/paper-trading-platform/main/deploy/install.sh | sudo bash
```

Run it again to upgrade. [docs/deployment.md](docs/deployment.md) covers the options,
configuration, daily operation, backups and HTTPS. [docs/proxmox.md](docs/proxmox.md)
shows the same on a Proxmox VE container, with the record of the test run.
[docs/hostinger.md](docs/hostinger.md) covers a Hostinger VPS with a domain and HTTPS.

## Settings

Set these as environment variables or in `backend/.env`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/app.db` | Any SQLAlchemy async URL. For Postgres use `postgresql+asyncpg://…` and add the `asyncpg` package. |
| `CHAIN_CRYPTO` | `okx,kraken,coinbase,yahoo` | Price sources for crypto, tried in order. Any ccxt exchange id works. |
| `CHAIN_STOCKS`, `CHAIN_ETFS`, `CHAIN_COMMODITIES`, `CHAIN_FOREX` | `yahoo` | Price sources for the other asset classes. |
| `COOKIE_SECURE` | `false` | Set to `true` when serving over HTTPS. |
| `AUTO_MIGRATE` | `true` | Apply database migrations at startup. Otherwise run `uv run alembic upgrade head`. |
| `RUN_MATCHER` | `true` | Evaluate open orders and write daily value points in the background. |
| `CHECK_ASSETS_ON_STARTUP` | `true` | Check at startup, and daily, which source prices each asset. |

An asset is priced by the first source in its chain that lists it. When that source
fails three times in a row it is skipped for a minute and the next one answers.

## What the simulation does and does not do

- Market, limit and stop orders; long only, fully funded, filled all or nothing.
- A market order fills at the ask (buy) or bid (sell). Where a source gives no bid
  and ask, the last price is moved by half the fee profile's assumed spread.
- A resting limit order fills at its limit price. A stop order fills at the quote that
  triggered it, which can be worse than the stop price.
- Orders fill only while the market is open and the quote is fresh: at most
  30 seconds old for crypto, 20 minutes for everything else.
- Yahoo quotes can be delayed by up to 15 minutes. Each trade records the source and
  the time its price was observed.
- Commodities are unleveraged exposure to the front-month futures price. Contract
  rolls are not adjusted.
- Stock splits and dividends are not applied to positions.
- USDT is treated as USD one to one.
- Yahoo Finance data is unofficial and licensed for personal use. Replace it with a
  licensed source before offering the platform to the public.

### Seed data

- `backend/app/seed/assets.yaml` — the ten assets per class, with the listing each
  ranking was checked against and the date.
- `backend/app/seed/fee_profiles.yaml` — the fee profiles, with the published
  schedule each was taken from and the date it was checked.

Edit either file and restart to apply it. Existing assets are never removed, and
trades already made keep the fee they were charged.

## Develop

```sh
cd backend
uv run pytest              # offline tests with scripted price sources
uv run pytest -m live      # adapter tests against OKX and Yahoo Finance
uv run ruff check
uv run alembic revision --autogenerate -m "describe the change"

cd frontend
npm run typecheck
npm test
npm run lint
npm run gen:api            # regenerate src/api/schema.d.ts from the running backend
```

Live adapter tests last passed on 2026-09-30 (5 of 5: OKX BTC/USDT; Yahoo AAPL, SPY,
GC=F and EURUSD=X).

The plan this was built from is in `openspec/changes/add-paper-trading-platform/`.

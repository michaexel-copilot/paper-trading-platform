import asyncio
import shutil
from decimal import Decimal

import httpx
import pytest
from argon2 import PasswordHasher
from sqlalchemy import select, text

from app import auth
from app.catalog import seed_assets
from app.config import ASSET_CLASSES, Settings
from app.db import make_engine, make_session_factory
from app.feeprofiles import seed_fee_profiles
from app.main import create_app
from app.marketdata.router import SourceRouter
from app.marketdata.service import MarketService
from app.models import Asset, Base
from tests.fakes import Clock, FakeSource

PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True, scope="session")
def cheap_password_hashing():
    """Argon2id with minimal cost: the tests check behaviour, not hashing strength."""
    original = auth._hasher, auth._DUMMY_HASH
    auth._hasher = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    auth._DUMMY_HASH = auth._hasher.hash("not-a-real-password")
    yield
    auth._hasher, auth._DUMMY_HASH = original


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def sources(clock) -> dict[str, FakeSource]:
    """Fakes under the real source names, so the seeded symbol mappings apply."""
    return {name: FakeSource(name, clock) for name in ("okx", "kraken", "yahoo")}


@pytest.fixture
def market(sources, clock) -> MarketService:
    chains = {cls: ["yahoo"] for cls in ASSET_CLASSES} | {"crypto": ["okx", "kraken", "yahoo"]}
    return MarketService(SourceRouter(sources, chains), now=clock, monotonic=clock.monotonic)


def _sqlite_url(path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


@pytest.fixture(scope="session")
def template_db(tmp_path_factory):
    """A database with the schema and the seed data, built once and copied per test."""
    path = tmp_path_factory.mktemp("template") / "template.db"

    async def build():
        engine = make_engine(_sqlite_url(path))
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with make_session_factory(engine)() as session:
            await seed_assets(session)
            await seed_fee_profiles(session)
            await session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        await engine.dispose()

    asyncio.run(build())
    return path


@pytest.fixture
def settings(tmp_path, template_db) -> Settings:
    database = tmp_path / "test.db"
    shutil.copy(template_db, database)
    return Settings(
        database_url=_sqlite_url(database),
        auto_migrate=False,
        run_matcher=False,
        check_assets_on_startup=False,
        lock_file=str(tmp_path / "backend.lock"),
        frontend_dist=str(tmp_path / "no-frontend"),
        # Most tests exercise behaviour unrelated to HED-42's platform separation and
        # don't set BETRIEBSART/ARTEFAKT_ART; tests/test_startgatter.py turns this
        # back on explicitly to test the gate itself.
        enforce_startgatter=False,
    )


@pytest.fixture
def betriebsart(monkeypatch):
    """Set BETRIEBSART for the duration of a test and clear the process-wide cache.

    ``app.betriebsart.get_betriebsart`` is ``lru_cache``-d on purpose (Auflage 1: read
    once, never again) -- tests that exercise more than one betriebsart must clear it
    themselves between reads, which is what this fixture does on teardown too.
    """
    from app.betriebsart import get_betriebsart

    def _set(wert: str) -> None:
        monkeypatch.setenv("BETRIEBSART", wert)
        get_betriebsart.cache_clear()

    yield _set
    get_betriebsart.cache_clear()


@pytest.fixture
async def app(settings, market):
    """The application with a fresh database per test and scripted price sources."""
    application = create_app(settings, market)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
def session_factory(app):
    return app.state.session_factory


@pytest.fixture
async def session(session_factory):
    async with session_factory() as db:
        yield db


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def anonymous(app):
    async with _client(app) as http:
        yield http


async def sign_up(http: httpx.AsyncClient, email: str) -> dict:
    response = await http.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
async def client(app):
    """A signed-in client."""
    async with _client(app) as http:
        await sign_up(http, "alice@example.com")
        yield http


@pytest.fixture
async def other_client(app):
    """A second signed-in user."""
    async with _client(app) as http:
        await sign_up(http, "bob@example.com")
        yield http


@pytest.fixture
def prices(sources):
    """Quotes for one asset per source and the EUR/USD rate."""
    sources["okx"].add("BTC/USDT", currency="USDT", step="0.00000001", minimum="0.00001")
    sources["okx"].price("BTC/USDT", 50000, bid=49990, ask=50010)
    sources["yahoo"].add("AAPL", "equity", exchange="NMS")
    sources["yahoo"].price("AAPL", 200, bid=199.9, ask=200.1)
    sources["yahoo"].add("EURUSD=X", "currency", exchange="CCY")
    sources["yahoo"].price("EURUSD=X", "1.25")
    return sources


async def asset_id(session, symbol: str) -> int:
    return await session.scalar(select(Asset.id).where(Asset.symbol == symbol))


async def create_portfolio(
    http: httpx.AsyncClient, name="Main", currency="USD", cash="10000"
) -> dict:
    response = await http.post(
        "/api/portfolios",
        json={"name": name, "base_currency": currency, "starting_cash": cash},
    )
    assert response.status_code == 201, response.text
    return response.json()


_order_counter = iter(range(1, 1_000_000))


async def place(http: httpx.AsyncClient, portfolio_id: int, **order) -> httpx.Response:
    order.setdefault("type", "market")
    order.setdefault("side", "buy")
    order.setdefault("client_order_id", f"test-order-{next(_order_counter):06d}")
    order["quantity"] = str(order["quantity"])
    for key in ("limit_price", "stop_price"):
        if key in order:
            order[key] = str(order[key])
    return await http.post(f"/api/portfolios/{portfolio_id}/orders", json=order)


def D(value) -> Decimal:
    return Decimal(str(value))

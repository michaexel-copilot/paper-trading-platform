import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from app.api import assets, auth, market, orders, portfolios
from app.catalog import check_all_assets, make_price_recorder, seed_assets
from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory
from app.feeprofiles import seed_fee_profiles
from app.marketdata.ccxt_adapter import CcxtSource
from app.marketdata.router import SourceRouter
from app.marketdata.service import MarketService
from app.marketdata.yahoo_adapter import YahooSource
from app.trading.matcher import Matcher, ProcessLock

log = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parent.parent
ASSET_CHECK_INTERVAL_SECONDS = 24 * 3600


def build_market(settings: Settings) -> MarketService:
    """One source object per name used in any chain: Yahoo, or a ccxt exchange id."""
    chains = settings.chains()
    names = {name for chain in chains.values() for name in chain}
    sources = {name: YahooSource() if name == "yahoo" else CcxtSource(name) for name in names}
    return MarketService(SourceRouter(sources, chains))


def run_migrations(database_url: str) -> None:
    from alembic.config import Config

    from alembic import command

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["database_url"] = database_url
    config.attributes["skip_logging"] = True
    command.upgrade(config, "head")


async def _check_assets_daily(session_factory, market_service: MarketService) -> None:
    while True:
        try:
            await check_all_assets(session_factory, market_service)
        except Exception:
            log.exception("asset availability check failed")
        await asyncio.sleep(ASSET_CHECK_INTERVAL_SECONDS)


def create_app(settings: Settings | None = None, market_service: MarketService | None = None):
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        lock = ProcessLock(settings.lock_file)
        if settings.run_matcher:
            lock.acquire()
        if settings.auto_migrate:
            # Alembic drives its own event loop, so it runs in a worker thread.
            await asyncio.to_thread(run_migrations, settings.database_url)

        engine = make_engine(settings.database_url)
        session_factory = make_session_factory(engine)
        async with session_factory() as session:
            await seed_assets(session)
            await seed_fee_profiles(session)

        service = market_service or build_market(settings)
        service.recorder = make_price_recorder(session_factory)
        matcher = Matcher(session_factory, service)

        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.market = service
        app.state.matcher = matcher

        background = []
        if settings.check_assets_on_startup:
            background.append(asyncio.create_task(_check_assets_daily(session_factory, service)))
        if settings.run_matcher:
            matcher.start()
        try:
            yield
        finally:
            await matcher.stop()
            for task in background:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await service.close()
            await engine.dispose()
            lock.release()

    app = FastAPI(title="Paper Trading Platform", lifespan=lifespan)
    app.state.settings = settings

    @app.get("/api/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    for module in (auth, assets, market, portfolios, orders):
        app.include_router(module.router)

    _serve_frontend(app, (BACKEND_DIR / settings.frontend_dist).resolve())
    return app


def _serve_frontend(app: FastAPI, dist: Path) -> None:
    """Serve the built single-page app, with its index for client-side routes."""
    index = dist / "index.html"

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str):
        if path.startswith("api/") or path == "api" or not index.is_file():
            raise HTTPException(404, "Not found")
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(dist):
            return FileResponse(candidate)
        return FileResponse(index)


app = create_app()

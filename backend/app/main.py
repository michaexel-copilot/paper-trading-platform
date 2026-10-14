import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.api import assets, auth, dashboard, market, orders, portfolios
from app.catalog import check_all_assets, make_price_recorder, seed_assets
from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory
from app.feeprofiles import seed_fee_profiles
from app.marketdata.ccxt_adapter import CcxtSource
from app.marketdata.router import SourceRouter
from app.marketdata.service import MarketService
from app.marketdata.yahoo_adapter import YahooSource
from app.models import BoardNotification, Proposal
from app.models.proposals import STATE_WARTET_AUF_BOARD
from app.notify import notify_new_proposal
from app.schluessel import pruefe_schluesseltrennung_beim_start
from app.schluesseltrennung_laufend import pruefe_einmal as pruefe_schluesseltrennung_einmal
from app.startgatter import pruefe_installation_marker, pruefe_matcher_fuer_live_artefakt
from app.trading.matcher import Matcher, ProcessLock
from app.trading.proposals import FRISTWAECHTER_INTERVAL_SECONDS, verfalle_ueberfaellige_vorschlaege

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


async def _expire_overdue_proposals_loop(session_factory) -> None:
    """Fristwächter-Schleife (HED-45, Entwurf 7.1). ``verfalle_ueberfaellige_vorschlaege``
    fängt Fehler je Vorschlag bereits selbst ab; dieses try/except ist nur gegen einen
    unerwarteten Fehler im Sweep selbst (z. B. DB-Verbindung weg)."""
    while True:
        try:
            await verfalle_ueberfaellige_vorschlaege(session_factory)
        except Exception:
            log.exception("Vorschlag-Fristwächter fehlgeschlagen")
        await asyncio.sleep(FRISTWAECHTER_INTERVAL_SECONDS)


async def _pruefe_schluesseltrennung_laufend(session_factory, interval_seconds: int) -> None:
    """Laufende Prüfung (HED-55, Entwurf Abschnitt 3.2): dieselbe Prüfung wie beim
    Start, in festem Intervall während des Betriebs. ``pruefe_schluesseltrennung_einmal``
    setzt bei Fehlschlag selbst das firmenweite Sperrkennzeichen; dieses try/except ist
    nur gegen einen unerwarteten Fehler im Sweep selbst (analog zu den Schleifen oben)."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await pruefe_schluesseltrennung_einmal(session_factory)
        except Exception:
            log.exception("Laufende Schlüsseltrennungs-Prüfung fehlgeschlagen")


async def _notify_new_proposals(session_factory, settings: Settings) -> None:
    """Board-Benachrichtigung je Vorschlag, der neu in Z3 steht (HED-43, Entwurf 5.3).

    Einziger Baustein, der diese Mails versendet (parallel zum Matcher, unter demselben
    ``run_matcher``-Prozesslauf) -- so entsteht bei mehreren Prozessen derselben
    Installation kein doppelter Versand. ``notify_new_proposal`` legt die
    ``BoardNotification``-Zeile selbst transaktional an, bevor sie sendet, und ist damit
    auch gegen zwei sich überlappende Durchläufe dieser Schleife sicher.
    """
    while True:
        try:
            async with session_factory() as session:
                already_notified = select(BoardNotification.proposal_id)
                pending = await session.scalars(
                    select(Proposal.id)
                    .where(Proposal.state == STATE_WARTET_AUF_BOARD)
                    .where(Proposal.id.not_in(already_notified))
                )
                proposal_ids = list(pending)
            for proposal_id in proposal_ids:
                await notify_new_proposal(session_factory, settings, proposal_id)
        except Exception:
            log.exception("Board-Benachrichtigung fehlgeschlagen")
        await asyncio.sleep(settings.notify_poll_seconds)


def create_app(settings: Settings | None = None, market_service: MarketService | None = None):
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.enforce_startgatter:
            # Beide Prüfungen brauchen keine Datenbank-Session und laufen deshalb vor
            # jeder anderen Startarbeit -- so früh wie technisch möglich abgelehnt
            # (Auflage 2 Startgatter, Auflage 4 Startgatter 4, CRO-Nachforderung HED-42).
            pruefe_matcher_fuer_live_artefakt(settings.run_matcher)
            await pruefe_schluesseltrennung_beim_start()
        lock = ProcessLock(settings.lock_file)
        if settings.run_matcher:
            lock.acquire()
        if settings.auto_migrate:
            # Alembic drives its own event loop, so it runs in a worker thread.
            await asyncio.to_thread(run_migrations, settings.database_url)

        engine = make_engine(settings.database_url)
        session_factory = make_session_factory(engine)
        if settings.enforce_startgatter:
            async with session_factory() as session:
                try:
                    await pruefe_installation_marker(session)
                except Exception:
                    # Auch BetriebsartFehler (fehlende/ungültige BETRIEBSART) läuft
                    # hier durch, nicht nur StartgatterFehler selbst.
                    log.exception("Startgatter abgelehnt -- Prozess startet nicht")
                    await engine.dispose()
                    lock.release()
                    raise
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
        if settings.check_proposal_expiry_on_startup:
            background.append(asyncio.create_task(_expire_overdue_proposals_loop(session_factory)))
        if settings.enforce_startgatter and settings.schluesseltrennung_laufend_pruefen:
            background.append(
                asyncio.create_task(
                    _pruefe_schluesseltrennung_laufend(
                        session_factory, settings.schluesseltrennung_intervall_sekunden
                    )
                )
            )
        if settings.run_matcher:
            matcher.start()
            background.append(asyncio.create_task(_notify_new_proposals(session_factory, settings)))
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

    for module in (auth, assets, market, portfolios, orders, dashboard):
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

import pytest
from sqlalchemy import select

from app.config import Settings
from app.main import create_app
from app.models.installation import InstallationMarker
from app.startgatter import (
    StartgatterFehler,
    lese_artefakt_art,
    pruefe_installation_marker,
    pruefe_matcher_fuer_live_artefakt,
)


def test_enforce_startgatter_ist_per_default_aus():
    """Diese Plattform hat auch Deployments ohne Hedgeclip-Bezug (docs/hostinger.md,
    docs/proxmox.md), die BETRIEBSART/ARTEFAKT_ART nie gesetzt haben. Jede Hedgeclip-
    Installation muss dies explizit anschalten -- siehe Issue-Kommentar HED-42."""
    assert Settings().enforce_startgatter is False


def test_fehlende_artefakt_art_verweigert_start(monkeypatch):
    monkeypatch.delenv("ARTEFAKT_ART", raising=False)
    with pytest.raises(StartgatterFehler):
        lese_artefakt_art()


def test_unbekannte_artefakt_art_verweigert_start(monkeypatch):
    monkeypatch.setenv("ARTEFAKT_ART", "irgendwas")
    with pytest.raises(StartgatterFehler):
        lese_artefakt_art()


async def test_leere_datenbank_wird_markiert(betriebsart, monkeypatch, session):
    betriebsart("papier")
    monkeypatch.setenv("ARTEFAKT_ART", "papier_artefakt")
    await pruefe_installation_marker(session)
    marker = await session.scalar(select(InstallationMarker))
    assert marker.betriebsart == "papier"
    assert marker.artefakt_art == "papier_artefakt"


async def test_zweiter_lauf_mit_gleicher_betriebsart_startet(betriebsart, monkeypatch, session):
    betriebsart("papier")
    monkeypatch.setenv("ARTEFAKT_ART", "papier_artefakt")
    await pruefe_installation_marker(session)
    await pruefe_installation_marker(session)  # zweiter Start, gleiche Installation


async def test_live_prozess_gegen_papier_markierte_datenbank_startet_nicht(
    betriebsart, monkeypatch, session
):
    betriebsart("papier")
    monkeypatch.setenv("ARTEFAKT_ART", "papier_artefakt")
    await pruefe_installation_marker(session)

    betriebsart("live")
    monkeypatch.setenv("ARTEFAKT_ART", "live_artefakt")
    with pytest.raises(StartgatterFehler):
        await pruefe_installation_marker(session)


async def test_papier_prozess_mit_live_artefakt_startet_nicht(betriebsart, monkeypatch, session):
    betriebsart("papier")
    monkeypatch.setenv("ARTEFAKT_ART", "live_artefakt")
    with pytest.raises(StartgatterFehler):
        await pruefe_installation_marker(session)


async def test_papier_artefakt_auf_live_betriebsart_startet_nicht(
    betriebsart, monkeypatch, session
):
    betriebsart("live")
    monkeypatch.setenv("ARTEFAKT_ART", "papier_artefakt")
    with pytest.raises(StartgatterFehler):
        await pruefe_installation_marker(session)


async def test_app_startet_nicht_ohne_betriebsart_wenn_gatter_an(
    betriebsart, monkeypatch, tmp_path, market
):
    """End-to-End: enforce_startgatter=True und keine BETRIEBSART gesetzt -- der
    gesamte Prozess (nicht nur die Einzelfunktion) startet nicht."""
    from app.betriebsart import BetriebsartFehler

    monkeypatch.delenv("BETRIEBSART", raising=False)
    monkeypatch.setenv("ARTEFAKT_ART", "papier_artefakt")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        auto_migrate=True,
        run_matcher=False,
        check_assets_on_startup=False,
        lock_file=str(tmp_path / "backend.lock"),
        frontend_dist=str(tmp_path / "no-frontend"),
        enforce_startgatter=True,
    )
    application = create_app(settings, market)
    with pytest.raises(BetriebsartFehler):
        async with application.router.lifespan_context(application):
            pass


def test_live_artefakt_mit_laufendem_matcher_verweigert_start(monkeypatch):
    """CRO-Nachforderung HED-42: der Papier-Matcher liegt nicht in app/trading/papier
    und bleibt deshalb im Live-Artefakt, wenn RUN_MATCHER nicht geprüft wird."""
    monkeypatch.setenv("ARTEFAKT_ART", "live_artefakt")
    with pytest.raises(StartgatterFehler):
        pruefe_matcher_fuer_live_artefakt(run_matcher=True)


def test_live_artefakt_ohne_matcher_startet(monkeypatch):
    monkeypatch.setenv("ARTEFAKT_ART", "live_artefakt")
    pruefe_matcher_fuer_live_artefakt(run_matcher=False)


def test_papier_artefakt_mit_laufendem_matcher_startet(monkeypatch):
    monkeypatch.setenv("ARTEFAKT_ART", "papier_artefakt")
    pruefe_matcher_fuer_live_artefakt(run_matcher=True)


async def test_app_startet_nicht_mit_matcher_in_live_artefakt(
    betriebsart, monkeypatch, tmp_path, market
):
    """End-to-End: live_artefakt + RUN_MATCHER=true -- der gesamte Prozess (nicht nur
    die Einzelfunktion) startet nicht, noch bevor die Matcher-Sperrdatei angefasst wird."""
    betriebsart("live")
    monkeypatch.setenv("ARTEFAKT_ART", "live_artefakt")
    monkeypatch.setenv("OKX_LIVE_API_KEY", "live-key")
    monkeypatch.setenv("OKX_LIVE_API_SECRET", "live-secret")
    monkeypatch.delenv("OKX_DEMO_API_KEY", raising=False)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        auto_migrate=True,
        run_matcher=True,
        check_assets_on_startup=False,
        lock_file=str(tmp_path / "backend.lock"),
        frontend_dist=str(tmp_path / "no-frontend"),
        enforce_startgatter=True,
    )
    application = create_app(settings, market)
    with pytest.raises(StartgatterFehler):
        async with application.router.lifespan_context(application):
            pass
    assert not (tmp_path / "backend.lock").exists()

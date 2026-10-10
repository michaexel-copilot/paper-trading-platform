import pytest

from app.config import Settings
from app.main import create_app
from app.schluessel import (
    SchluesselFehler,
    pruefe_schluesseltrennung_beim_start,
    read_exchange_credential,
)


def test_kein_schluessel_lesbar_verweigert(betriebsart, monkeypatch):
    betriebsart("papier")
    monkeypatch.delenv("OKX_DEMO_API_KEY", raising=False)
    monkeypatch.delenv("OKX_DEMO_API_SECRET", raising=False)
    monkeypatch.delenv("OKX_LIVE_API_KEY", raising=False)
    with pytest.raises(SchluesselFehler):
        read_exchange_credential("okx")


def test_papier_liest_eigenen_schluessel(betriebsart, monkeypatch):
    betriebsart("papier")
    monkeypatch.setenv("OKX_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("OKX_DEMO_API_SECRET", "demo-secret")
    monkeypatch.delenv("OKX_LIVE_API_KEY", raising=False)
    credential = read_exchange_credential("okx")
    assert credential.api_key == "demo-key"
    assert credential.betriebsart.value == "papier"
    assert credential.simulated_trading_header is True


def test_live_liest_eigenen_schluessel(betriebsart, monkeypatch):
    betriebsart("live")
    monkeypatch.setenv("OKX_LIVE_API_KEY", "live-key")
    monkeypatch.setenv("OKX_LIVE_API_SECRET", "live-secret")
    monkeypatch.delenv("OKX_DEMO_API_KEY", raising=False)
    credential = read_exchange_credential("okx")
    assert credential.api_key == "live-key"
    assert credential.betriebsart.value == "live"
    assert credential.simulated_trading_header is False


def test_papier_mit_lesbarem_live_schluessel_ist_sofort_stopp(betriebsart, monkeypatch):
    """Das benannte Hauptrisiko: ein Live-Schlüssel ist auf dem Papier-Host lesbar."""
    betriebsart("papier")
    monkeypatch.setenv("OKX_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("OKX_DEMO_API_SECRET", "demo-secret")
    monkeypatch.setenv("OKX_LIVE_API_KEY", "ein-live-schluessel-der-hier-nicht-sein-darf")
    with pytest.raises(SchluesselFehler):
        read_exchange_credential("okx")


def test_live_mit_lesbarem_demo_schluessel_schlaegt_an(betriebsart, monkeypatch):
    betriebsart("live")
    monkeypatch.setenv("OKX_LIVE_API_KEY", "live-key")
    monkeypatch.setenv("OKX_LIVE_API_SECRET", "live-secret")
    monkeypatch.setenv("OKX_DEMO_API_KEY", "demo-key-sollte-hier-nicht-lesbar-sein")
    with pytest.raises(SchluesselFehler):
        read_exchange_credential("okx")


def test_start_selbsttest_ohne_jeden_schluessel_prueft_nichts(betriebsart, monkeypatch):
    """Eine Installation ohne jede Gegenstellen-Anbindung hat nichts zu prüfen --
    kein falscher Alarm, nur weil noch kein Schlüssel konfiguriert ist."""
    betriebsart("papier")
    monkeypatch.delenv("OKX_DEMO_API_KEY", raising=False)
    monkeypatch.delenv("OKX_DEMO_API_SECRET", raising=False)
    monkeypatch.delenv("OKX_LIVE_API_KEY", raising=False)
    pruefe_schluesseltrennung_beim_start()


def test_start_selbsttest_findet_fremden_schluessel_ohne_jeden_versand(betriebsart, monkeypatch):
    """CRO-Nachforderung HED-42: die Querkontaminationsprüfung muss beim Prozessstart
    greifen, auch wenn nie eine Order über den Versand-Engpass läuft und
    ``read_exchange_credential`` deshalb sonst nie aufgerufen würde."""
    betriebsart("papier")
    monkeypatch.setenv("OKX_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("OKX_DEMO_API_SECRET", "demo-secret")
    monkeypatch.setenv("OKX_LIVE_API_KEY", "ein-live-schluessel-der-hier-nicht-sein-darf")
    with pytest.raises(SchluesselFehler):
        pruefe_schluesseltrennung_beim_start()


def test_start_selbsttest_mit_nur_eigenem_schluessel_startet(betriebsart, monkeypatch):
    betriebsart("papier")
    monkeypatch.setenv("OKX_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("OKX_DEMO_API_SECRET", "demo-secret")
    monkeypatch.delenv("OKX_LIVE_API_KEY", raising=False)
    pruefe_schluesseltrennung_beim_start()


async def test_app_startet_nicht_mit_fremdem_schluessel_lesbar(
    betriebsart, monkeypatch, tmp_path, market
):
    """End-to-End: der echte Prozessstart (app.main.create_app) ruft den Start-
    Selbsttest auf -- nicht nur eine isoliert aufgerufene Funktion. Reproduziert das
    benannte Hauptrisiko: eine Papier-Installation, auf der ein Live-Schlüssel lesbar
    ist, obwohl nie eine Order abgesetzt wird."""
    betriebsart("papier")
    monkeypatch.setenv("ARTEFAKT_ART", "papier_artefakt")
    monkeypatch.setenv("OKX_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("OKX_DEMO_API_SECRET", "demo-secret")
    monkeypatch.setenv("OKX_LIVE_API_KEY", "ein-live-schluessel-der-hier-nicht-sein-darf")
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
    with pytest.raises(SchluesselFehler):
        async with application.router.lifespan_context(application):
            pass

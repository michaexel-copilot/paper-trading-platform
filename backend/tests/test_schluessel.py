from types import SimpleNamespace

import pytest

from app import schluessel
from app.betriebsart import Betriebsart
from app.config import Settings
from app.main import create_app
from app.schluessel import (
    ExchangeCredential,
    SchluesselFehler,
    pruefe_schluesseltrennung_beim_start,
    read_exchange_credential,
    selbsttest_bestanden,
    verifiziere_kontostand,
)


def _credential(**overrides) -> ExchangeCredential:
    felder = dict(
        exchange="okx",
        api_key="demo-key",
        api_secret="demo-secret",
        passphrase="demo-passphrase",
        betriebsart=Betriebsart.PAPIER,
        simulated_trading_header=True,
    )
    felder.update(overrides)
    return ExchangeCredential(**felder)


def _stelle_kontostand_leser(monkeypatch, *, erfolgreich: bool):
    """Ersetzt ``schluessel._echter_kontostand_leser`` durch einen Fake ohne
    Netzzugriff -- analog zu ``monkeypatch.setattr(okx_demo, "_client", ...)`` in
    test_okx_demo.py. ``erfolgreich=False`` simuliert jeden fehlgeschlagenen
    Lesevorgang (Timeout/Ablehnung/falsches Format/kein Live-Pfad) -- die Gegenstelle
    selbst entscheidet darüber (siehe ``tests/test_okx_demo_live.py``), nicht ein
    Feldvergleich auf dieser Seite (vgl. HED-101-Nachforderung in ``app/schluessel.py``,
    ``verifiziere_kontostand``)."""

    async def fake(_credential):
        return SimpleNamespace(betriebsart=_credential.betriebsart.value) if erfolgreich else None

    monkeypatch.setattr(schluessel, "_echter_kontostand_leser", fake)


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


async def test_start_selbsttest_ohne_jeden_schluessel_prueft_nichts(betriebsart, monkeypatch):
    """Eine Installation ohne jede Gegenstellen-Anbindung hat nichts zu prüfen --
    kein falscher Alarm, nur weil noch kein Schlüssel konfiguriert ist."""
    betriebsart("papier")
    monkeypatch.delenv("OKX_DEMO_API_KEY", raising=False)
    monkeypatch.delenv("OKX_DEMO_API_SECRET", raising=False)
    monkeypatch.delenv("OKX_LIVE_API_KEY", raising=False)
    await pruefe_schluesseltrennung_beim_start()


async def test_start_selbsttest_findet_fremden_schluessel_ohne_jeden_versand(
    betriebsart, monkeypatch
):
    """CRO-Nachforderung HED-42: die Querkontaminationsprüfung muss beim Prozessstart
    greifen, auch wenn nie eine Order über den Versand-Engpass läuft und
    ``read_exchange_credential`` deshalb sonst nie aufgerufen würde. Schlägt bereits in
    Punkt 1-3 fehl, bevor Punkt 4 (Kontostand) je versucht wird -- kein Netzzugriff
    nötig, deshalb kein Fake für den Kontostand-Leser."""
    betriebsart("papier")
    monkeypatch.setenv("OKX_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("OKX_DEMO_API_SECRET", "demo-secret")
    monkeypatch.setenv("OKX_LIVE_API_KEY", "ein-live-schluessel-der-hier-nicht-sein-darf")
    with pytest.raises(SchluesselFehler):
        await pruefe_schluesseltrennung_beim_start()


async def test_start_selbsttest_mit_nur_eigenem_schluessel_startet(betriebsart, monkeypatch):
    """HED-55: seit Punkt 4 dazukam, braucht ein bestehender Start-Selbsttest auch
    einen bestehenden Kontostand-Lesevorgang -- hier als Fake, kein echter Netzzugriff
    in diesem Test (siehe test_okx_demo.py für den echten Orderpfad/Kontostand)."""
    betriebsart("papier")
    monkeypatch.setenv("OKX_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("OKX_DEMO_API_SECRET", "demo-secret")
    monkeypatch.delenv("OKX_LIVE_API_KEY", raising=False)
    _stelle_kontostand_leser(monkeypatch, erfolgreich=True)
    await pruefe_schluesseltrennung_beim_start()


async def test_start_selbsttest_lehnt_ab_wenn_kontostand_nicht_verifizierbar(
    betriebsart, monkeypatch
):
    """Selbsttest-Punkt 4 (HED-55): ein Schlüssel, der zwar lesbar ist, sich aber
    nicht gegen die Gegenstelle verifizieren lässt (Timeout, Ablehnung, falsches
    Format, Umgebungsmismatch -- siehe tests/test_okx_demo_live.py für den echten
    Ablehnungsfall), verhindert den Start genauso wie eine Querkontamination."""
    betriebsart("papier")
    monkeypatch.setenv("OKX_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("OKX_DEMO_API_SECRET", "demo-secret")
    monkeypatch.delenv("OKX_LIVE_API_KEY", raising=False)
    _stelle_kontostand_leser(monkeypatch, erfolgreich=False)
    with pytest.raises(SchluesselFehler):
        await pruefe_schluesseltrennung_beim_start()


# --- verifiziere_kontostand (Selbsttest Punkt 4) ----------------------------------


async def test_verifiziere_kontostand_erfolg(monkeypatch):
    _stelle_kontostand_leser(monkeypatch, erfolgreich=True)
    assert await verifiziere_kontostand(_credential()) is True


async def test_verifiziere_kontostand_lesefehler_gilt_als_fehlschlag(monkeypatch):
    """Jeder fehlgeschlagene Lesevorgang -- gleich ob Timeout, Ablehnung, falsches
    Format oder Umgebungsmismatch -- ist fail closed. Welchen dieser Fälle OKX für
    einen echten Umgebungsmismatch tatsächlich meldet, ist Sache von
    tests/test_okx_demo_live.py, nicht dieses Fakes."""
    _stelle_kontostand_leser(monkeypatch, erfolgreich=False)
    assert await verifiziere_kontostand(_credential()) is False


async def test_echter_kontostand_leser_live_ohne_wiring_gibt_none():
    """Es gibt noch keinen Live-Orderpfad und keinen Live-Schlüssel -- Punkt 4 kann
    für Live deshalb nicht bestehen, nicht versehentlich durchrutschen."""
    credential = _credential(betriebsart=Betriebsart.LIVE, simulated_trading_header=False)
    assert await schluessel._echter_kontostand_leser(credential) is None


async def test_echter_kontostand_leser_papier_ist_auf_okx_demo_verdrahtet(monkeypatch):
    """Beweist, dass der deferred Import tatsächlich bei
    ``app.trading.papier.okx_demo.lese_kontostand`` ankommt -- nicht nur, dass der
    Fake in den obigen Tests korrekt eingesetzt wurde."""
    from app.trading.papier import okx_demo

    class FakeOkxClient:
        async def privateGetAccountBalance(self):
            return {"code": "0", "msg": "", "data": [{"totalEq": "1"}]}

        async def close(self):
            pass

    monkeypatch.setattr(okx_demo, "_client", lambda credential: FakeOkxClient())

    ergebnis = await schluessel._echter_kontostand_leser(_credential())

    assert ergebnis.quelle == "okx_demo"
    assert ergebnis.betriebsart == "papier"


# --- selbsttest_bestanden (laufende Prüfung, HED-55) ------------------------------


async def test_selbsttest_bestanden_true_wenn_alles_passt(betriebsart, monkeypatch):
    betriebsart("papier")
    monkeypatch.setenv("OKX_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("OKX_DEMO_API_SECRET", "demo-secret")
    monkeypatch.delenv("OKX_LIVE_API_KEY", raising=False)
    _stelle_kontostand_leser(monkeypatch, erfolgreich=True)
    assert await selbsttest_bestanden() is True


async def test_selbsttest_bestanden_false_bei_lesefehler(betriebsart, monkeypatch):
    betriebsart("papier")
    monkeypatch.setenv("OKX_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("OKX_DEMO_API_SECRET", "demo-secret")
    monkeypatch.delenv("OKX_LIVE_API_KEY", raising=False)
    _stelle_kontostand_leser(monkeypatch, erfolgreich=False)
    assert await selbsttest_bestanden() is False


async def test_selbsttest_bestanden_false_bei_querkontamination(betriebsart, monkeypatch):
    """Punkt 1-3 schlagen schon fehl -- Punkt 4 wird nie versucht, kein Fake nötig."""
    betriebsart("papier")
    monkeypatch.setenv("OKX_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("OKX_DEMO_API_SECRET", "demo-secret")
    monkeypatch.setenv("OKX_LIVE_API_KEY", "ein-live-schluessel-der-hier-nicht-sein-darf")
    assert await selbsttest_bestanden() is False


async def test_selbsttest_bestanden_true_ohne_jeden_schluessel(betriebsart, monkeypatch):
    betriebsart("papier")
    monkeypatch.delenv("OKX_DEMO_API_KEY", raising=False)
    monkeypatch.delenv("OKX_DEMO_API_SECRET", raising=False)
    monkeypatch.delenv("OKX_LIVE_API_KEY", raising=False)
    assert await selbsttest_bestanden() is True


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

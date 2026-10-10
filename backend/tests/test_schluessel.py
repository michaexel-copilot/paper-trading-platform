import pytest

from app.schluessel import SchluesselFehler, read_exchange_credential


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

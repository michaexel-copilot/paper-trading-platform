"""Tägliche externe Buch-Sicherung (B-16, HED-85, Plan Revision 4).

Kein echter ``sqlite3``- oder ``rclone``-Aufruf -- ``app.trading.buch_sicherung._run``
wird durch einen Fake ersetzt, analog zu ``app.notify.send_blocking`` in
``test_notify.py``. Der Fake schreibt für einen simulierten ``.backup``-Erfolg selbst
eine Datei an die Zielstelle, weil ``_sichere_blocking`` prüft, dass der Snapshot
tatsächlich entstanden ist.
"""

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select

from app import notify
from app.config import Settings
from app.db import make_engine, make_session_factory
from app.models import Base, BuchSicherungLauf
from app.trading import buch_sicherung


def _url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'buch.db').as_posix()}"


@pytest.fixture
async def session_factory(tmp_path):
    engine = make_engine(_url(tmp_path))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = make_session_factory(engine)
    yield factory
    await engine.dispose()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(database_url=_url(tmp_path), buch_sicherung_aktiv=True)


@pytest.fixture(autouse=True)
def papier_betriebsart(betriebsart):
    betriebsart("papier")


@pytest.fixture(autouse=True)
def no_real_mail(monkeypatch):
    """Kein echter SMTP-Versand in dieser Datei -- separat in test_notify.py geprüft."""
    monkeypatch.setattr(notify, "send_blocking", lambda s, msg: None)


def _fake_run(*, sqlite_ok: bool, rclone_ok: bool, calls: list[list[str]]):
    def fake(cmd: list[str]) -> subprocess.CompletedProcess:
        calls.append(cmd)
        if cmd[0] == "sqlite3":
            # cmd[2] == ".backup <pfad>"
            ziel = cmd[2].split(" ", 1)[1]
            if not sqlite_ok:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="sqlite3 boom")
            Path(ziel).write_bytes(b"fake-snapshot")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if not rclone_ok:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="rclone boom")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return fake


async def _einziger_lauf(session_factory) -> BuchSicherungLauf:
    async with session_factory() as session:
        rows = list(await session.scalars(select(BuchSicherungLauf)))
        assert len(rows) == 1
        return rows[0]


async def test_erfolgreiche_sicherung_schreibt_protokollzeile(
    session_factory, settings, monkeypatch
):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        buch_sicherung, "_run", _fake_run(sqlite_ok=True, rclone_ok=True, calls=calls)
    )

    ok = await buch_sicherung.sichere_einmal(session_factory, settings)

    assert ok is True
    lauf = await _einziger_lauf(session_factory)
    assert lauf.ergebnis == "erfolg"
    assert lauf.bytes_gesichert == len(b"fake-snapshot")
    assert lauf.beendet_at is not None
    assert lauf.ziel_remote.startswith("onedrive-crypt:papier/")
    assert lauf.dateiname.startswith("app.db-") and lauf.dateiname.endswith(".db")

    rclone_aufrufe = [c for c in calls if c[0] == "rclone"]
    assert len(rclone_aufrufe) == 1
    assert rclone_aufrufe[0][1] == "copy"


async def test_sqlite_backup_fehlschlag_wird_gemeldet(session_factory, settings, monkeypatch):
    gesendete_mails = []
    monkeypatch.setattr(notify, "send_blocking", lambda s, msg: gesendete_mails.append(msg))
    monkeypatch.setattr(
        buch_sicherung, "_run", _fake_run(sqlite_ok=False, rclone_ok=True, calls=[])
    )

    ok = await buch_sicherung.sichere_einmal(session_factory, settings)

    assert ok is False
    lauf = await _einziger_lauf(session_factory)
    assert lauf.ergebnis == "fehlgeschlagen"
    assert "sqlite3" in lauf.fehlertext
    assert lauf.cro_alert_gesendet is True
    assert len(gesendete_mails) == 1
    assert "B-16" in gesendete_mails[0]["Subject"]


async def test_rclone_copy_fehlschlag_wird_gemeldet(session_factory, settings, monkeypatch):
    monkeypatch.setattr(
        buch_sicherung, "_run", _fake_run(sqlite_ok=True, rclone_ok=False, calls=[])
    )

    ok = await buch_sicherung.sichere_einmal(session_factory, settings)

    assert ok is False
    lauf = await _einziger_lauf(session_factory)
    assert lauf.ergebnis == "fehlgeschlagen"
    assert "rclone copy" in lauf.fehlertext


async def test_abgelaufenes_refresh_token_wird_gemeldet_nicht_stumm(
    session_factory, settings, monkeypatch
):
    """Auflage 2 aus Board-Bescheid 21.10.2026 (HED-85, Kommentar f40c53f3): Verhalten bei
    abgelaufenem Refresh-Token muss "Fehler melden, kein stilles Scheitern" sein. rclone
    meldet das mit einem OAuth-Fehler und einem Exit-Code != 0 -- geht über denselben
    generischen Fehlerpfad wie jeder andere rclone-Fehlschlag (siehe
    ``test_rclone_copy_fehlschlag_wird_gemeldet``), hier mit dem konkreten Fehlerbild."""
    gesendete_mails = []
    monkeypatch.setattr(notify, "send_blocking", lambda s, msg: gesendete_mails.append(msg))
    oauth_fehler = (
        "oauth2: cannot fetch token: 400 Bad Request\n"
        'Response: {"error":"invalid_grant","error_description":"AADSTS700082: '
        'The refresh token has expired due to inactivity."}'
    )

    def fake(cmd: list[str]) -> subprocess.CompletedProcess:
        if cmd[0] == "sqlite3":
            ziel = cmd[2].split(" ", 1)[1]
            Path(ziel).write_bytes(b"fake-snapshot")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=oauth_fehler)

    monkeypatch.setattr(buch_sicherung, "_run", fake)

    ok = await buch_sicherung.sichere_einmal(session_factory, settings)

    assert ok is False
    lauf = await _einziger_lauf(session_factory)
    assert lauf.ergebnis == "fehlgeschlagen"
    assert "invalid_grant" in lauf.fehlertext
    assert lauf.cro_alert_gesendet is True
    assert len(gesendete_mails) == 1
    assert "B-16" in gesendete_mails[0]["Subject"]


async def test_rotation_nutzt_ausschliesslich_delete_mit_min_age_nie_sync(settings, monkeypatch):
    calls: list[list[str]] = []

    def fake(cmd: list[str]) -> subprocess.CompletedProcess:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(buch_sicherung, "_run", fake)

    ok = await buch_sicherung.rotiere_einmal(settings)

    assert ok is True
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[1] == "delete"
    assert "sync" not in cmd
    assert any(arg.startswith("--min-age=") for arg in cmd)
    assert f"--min-age={settings.buch_sicherung_rotation_tage}d" in cmd


def test_zielordner_trennt_papier_und_live(settings, monkeypatch):
    monkeypatch.setattr(buch_sicherung.socket, "gethostname", lambda: "host-a")

    papier = buch_sicherung.zielordner(settings, "papier")
    live = buch_sicherung.zielordner(settings, "live")

    assert papier == "onedrive-crypt:papier/host-a"
    assert live == "onedrive-crypt:live/host-a"
    assert papier != live


def test_sqlite_pfad_lehnt_andere_datenbanken_ab():
    with pytest.raises(buch_sicherung.BuchSicherungFehler):
        buch_sicherung._sqlite_pfad("postgresql+asyncpg://user:pw@host/db")

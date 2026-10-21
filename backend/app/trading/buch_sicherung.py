"""Tägliche externe Sicherung des Handelsbuchs (B-16, HED-85, Plan Revision 6
Abschnitt 2-4).

Positionen, Orders und das Freigabe-Protokoll liegen alle in derselben SQLite-Datei
(``Settings.database_url``) -- ein konsistenter ``sqlite3 .backup``-Snapshot deckt alle
drei im Issue geforderten Inhalte in einem Schritt ab, auch während der laufende
Prozess die Datei noch offen hält.

Verschlüsselung (rclone crypt) passiert, bevor irgendetwas den Host verlässt: dieser
Code ruft ausschließlich den konfigurierten Remote-Namen (``Settings.
onedrive_rclone_remote``, Default ``onedrive-crypt``) auf. Token und Crypt-Schlüssel
liegen in der rclone-Konfiguration auf dem Host, vom Board eingerichtet -- dieser
Prozess liest beides nie selbst (Plan Abschnitt 3).

Löschschutz (Plan Abschnitt 4a, Board-Auflage Kommentar 8bf1d2ac), bindend:
1. Ausschließlich ``rclone copy``, nie ``rclone sync`` -- ``copy`` fügt nur hinzu.
2. Rotation ausschließlich über das Dateialter im Namen (``--min-age``), nie über
   Abgleich gegen den Host-Zustand.
"""

import asyncio
import logging
import socket
import subprocess
import tempfile
from pathlib import Path

from app.betriebsart import get_betriebsart
from app.config import Settings
from app.dbtypes import utcnow
from app.models.buch_sicherung import BuchSicherungLauf
from app.notify import notify_backup_failure

log = logging.getLogger(__name__)

_SQLITE_PREFIX = "sqlite+aiosqlite:///"


class BuchSicherungFehler(Exception):
    pass


def _sqlite_pfad(database_url: str) -> Path:
    if not database_url.startswith(_SQLITE_PREFIX):
        raise BuchSicherungFehler(
            f"Nur SQLite-Datenbanken werden gesichert, nicht {database_url!r}."
        )
    return Path(database_url[len(_SQLITE_PREFIX) :])


def zielordner(settings: Settings, betriebsart: str) -> str:
    """``onedrive-crypt:papier/<hostname>`` bzw. ``.../live/<hostname>`` -- je
    Installation ein eigener Unterordner, keine Installation kennt den Pfad der
    anderen (Plan Abschnitt 3)."""
    praefix = settings.buch_sicherung_pfad_praefix.format(
        betriebsart=betriebsart, hostname=socket.gethostname()
    )
    remote = settings.onedrive_rclone_remote.rstrip(":")
    return f"{remote}:{praefix.strip('/')}"


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)


def _sichere_blocking(settings: Settings, quelle: Path, ziel: str, dateiname: str) -> int:
    """Blockierender Teil (sqlite3 + rclone), in einem Worker-Thread ausgeführt --
    gleiches Muster wie ``app.notify.send_blocking`` für smtplib. Gibt die Größe des
    Snapshots in Byte zurück."""
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / dateiname
        sqlite_ergebnis = _run(["sqlite3", str(quelle), f".backup {snapshot}"])
        if sqlite_ergebnis.returncode != 0:
            raise BuchSicherungFehler(
                f"sqlite3 .backup fehlgeschlagen: {sqlite_ergebnis.stderr.strip()}"
            )
        if not snapshot.exists():
            raise BuchSicherungFehler("sqlite3 .backup hat keine Datei erzeugt.")
        groesse = snapshot.stat().st_size
        # Ausschließlich "copy" -- siehe Modul-Docstring, Löschschutz Punkt 1.
        rclone_ergebnis = _run([settings.rclone_binary, "copy", str(snapshot), ziel])
        if rclone_ergebnis.returncode != 0:
            raise BuchSicherungFehler(
                f"rclone copy fehlgeschlagen: {rclone_ergebnis.stderr.strip()}"
            )
    return groesse


async def sichere_einmal(session_factory, settings: Settings) -> bool:
    """Eine Durchführung (analog ``stop_orderbuch_abgleich.pruefe_einmal``). Trägt
    immer eine ``BuchSicherungLauf``-Zeile ein -- auch bei Fehlschlag, als Nachweis
    für die CRO-Abnahme. Gibt zurück, ob die Sicherung geklappt hat."""
    betriebsart = get_betriebsart().value
    ziel = zielordner(settings, betriebsart)
    zeitstempel = utcnow().strftime("%Y%m%dT%H%M%SZ")
    dateiname = f"app.db-{zeitstempel}.db"

    async with session_factory() as session:
        lauf = BuchSicherungLauf(ziel_remote=ziel, dateiname=dateiname, ergebnis="fehlgeschlagen")
        try:
            quelle = _sqlite_pfad(settings.database_url)
            groesse = await asyncio.to_thread(_sichere_blocking, settings, quelle, ziel, dateiname)
        except Exception as exc:
            log.exception("Tägliche Buch-Sicherung fehlgeschlagen")
            lauf.beendet_at = utcnow()
            lauf.fehlertext = str(exc)[:500]
            session.add(lauf)
            await session.commit()
            lauf.cro_alert_gesendet = await notify_backup_failure(settings, lauf.fehlertext)
            await session.commit()
            return False

        lauf.beendet_at = utcnow()
        lauf.ergebnis = "erfolg"
        lauf.bytes_gesichert = groesse
        session.add(lauf)
        await session.commit()
    return True


async def rotiere_einmal(settings: Settings) -> bool:
    """Löscht im eigenen Zielordner ausschließlich Dateien älter als
    ``Settings.buch_sicherung_rotation_tage`` -- über rclones Alters-Filter, nie über
    Abgleich gegen den Host-Zustand (Löschschutz Punkt 2). Wird nur nach einer
    erfolgreichen Sicherung aufgerufen (siehe Schleife in ``app.main``), damit eine
    fehlschlagende Rotation nie die letzte verbliebene gültige Kopie entfernt, wenn die
    heutige Sicherung selbst schon gescheitert ist."""
    betriebsart = get_betriebsart().value
    ziel = zielordner(settings, betriebsart)
    ergebnis = await asyncio.to_thread(
        _run,
        [
            settings.rclone_binary,
            "delete",
            ziel,
            f"--min-age={settings.buch_sicherung_rotation_tage}d",
        ],
    )
    if ergebnis.returncode != 0:
        log.error("Rotation der Buch-Sicherung fehlgeschlagen: %s", ergebnis.stderr.strip())
        return False
    return True

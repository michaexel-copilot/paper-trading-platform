"""Täglicher Stop-Orderbuch-Abgleich (B-12, HED-84, Entwurf Abschnitt 10).

Vergleicht das eigene Stop-Buch (``StopOrderBuch``, Festlegung F-15 -- bewusst keine
Ableitung aus der Nur-Anfügen-Protokolltabelle ``VersandEngpassEintrag``) gegen die
offenen Algo-Order bei OKX-Demo. Jede Abweichung -- fehlend, Waise, Mengen- oder
Triggerabweichung -- und jedes Scheitern oder Ausbleiben des Abgleichs selbst setzen
sofort das firmenweite Sperrkennzeichen (Festlegung F-16). Keine Selbstheilung, keine
Schwelle, keine Wiederholungslogik -- eine Abweichung ist immer ein Hinweis auf einen
Fehler im Buchführungs- oder Sendepfad, nicht ein Betriebszustand (Entwurf 10.5).

Gebaut nach dem Muster von ``app.schluesseltrennung_laufend``: eine testbare
Durchführung (``pruefe_einmal``), die selbst bei Fehlschlag das Kennzeichen setzt. Die
Endlos-Schleife mit Intervall lebt in ``app.main``.
"""

import logging
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dbtypes import utcnow
from app.kill_switch import setze_firmenweit
from app.marketdata.service import MarketService
from app.models.stop_order import StopAbgleichLauf, StopOrderBuch
from app.trading.versandengpass import Versandengpass

log = logging.getLogger(__name__)

_ABWEICHUNG_AUSLOESER = "stop_orderbuch_abweichung"
_FEHLGESCHLAGEN_AUSLOESER = "stop_orderbuch_abgleich_fehlgeschlagen"

# Entwurf Abschnitt 10.4: zusätzlich zu den Symbolen mit aktuell offener eigener Zeile
# werden alle Symbole abgefragt, für die in diesem Zeitraum überhaupt ein Stop bestand
# -- sonst würde eine Waise in einem Symbol ohne eigene offene Zeile nie gefunden.
_WAISEN_RUECKBLICK = timedelta(days=30)


async def pruefe_einmal(
    session_factory,
    engpass: Versandengpass,
    *,
    exchange: str = "okx",
    mengen_rundungstoleranz: Decimal,
    totmannfrist: timedelta,
    market: MarketService | None = None,
    tranche: str = "B1",
) -> bool:
    """Eine Durchführung (Entwurf Abschnitt 10.1/10.3). Gibt zurück, ob der Abgleich
    bestanden wurde (``True``) -- bei Fehlschlag oder Abweichung wird bereits hier das
    firmenweite Sperrkennzeichen gesetzt, der Aufrufer muss das nicht zusätzlich tun.

    ``market``/``tranche`` (Pfad 2, HED-191 Abschnitt 1b): nur für den Storno-Sweep
    ruhender Einstiegsorders gebraucht, der unmittelbar nach jedem Setzen des
    Kennzeichens ausgelöst wird, siehe ``app.trading.storno_sweep``."""
    async with session_factory() as session:
        try:
            bestanden = await _fuehre_abgleich_durch(
                session, engpass, exchange, mengen_rundungstoleranz
            )
        except Exception as exc:
            log.exception("Stop-Orderbuch-Abgleich fehlgeschlagen")
            session.add(
                StopAbgleichLauf(
                    beendet_at=utcnow(),
                    ergebnis="fehlgeschlagen",
                    fehlertext=str(exc)[:500],
                )
            )
            await setze_firmenweit(
                session,
                ausloeser=_FEHLGESCHLAGEN_AUSLOESER,
                begruendung=f"Täglicher Stop-Orderbuch-Abgleich fehlgeschlagen: {exc}"[:1000],
            )
            await session.commit()
            await _sweepe_nach_sperre(session_factory, engpass, market, tranche)
            return False

    if not bestanden:
        await _sweepe_nach_sperre(session_factory, engpass, market, tranche)

    totmann_ausgeloest = await _pruefe_totmannschalter(session_factory, totmannfrist)
    if totmann_ausgeloest:
        await _sweepe_nach_sperre(session_factory, engpass, market, tranche)
    return bestanden


async def _sweepe_nach_sperre(
    session_factory, engpass: Versandengpass, market: MarketService | None, tranche: str
) -> None:
    from app.trading.storno_sweep import sweep_ruhende_einstiegsorders

    try:
        await sweep_ruhende_einstiegsorders(
            session_factory, engpass, market=market, tranche=tranche
        )
    except Exception:
        log.exception("Storno-Sweep nach Stop-Orderbuch-Abgleich-Sperre fehlgeschlagen (Pfad 2)")


async def _fuehre_abgleich_durch(
    session: AsyncSession,
    engpass: Versandengpass,
    exchange: str,
    mengen_rundungstoleranz: Decimal,
) -> bool:
    begonnen_at = utcnow()
    eigene_offene = list(
        await session.scalars(select(StopOrderBuch).where(StopOrderBuch.zustand == "offen"))
    )
    nachfuehrung_laufend = list(
        await session.scalars(
            select(StopOrderBuch).where(StopOrderBuch.zustand == "nachfuehrung_laeuft")
        )
    )

    symbole = {zeile.symbol for zeile in eigene_offene}
    symbole |= {
        zeile.symbol
        for zeile in await session.scalars(
            select(StopOrderBuch).where(
                StopOrderBuch.erstellt_at >= begonnen_at - _WAISEN_RUECKBLICK
            )
        )
    }

    eigene_nach_algo_id = {zeile.gegenstellen_algo_id: zeile for zeile in eigene_offene}
    nachfuehrung_algo_ids = {zeile.gegenstellen_algo_id for zeile in nachfuehrung_laufend}

    gegenstelle_gesamt = 0
    fehlend = []
    waisen = []
    mengenabweichung = []
    triggerabweichung = []
    gesehene_algo_ids: set[str] = set()

    for symbol in sorted(symbole):
        gegenstellen_zeilen = await engpass.lese_offene_stop_orders(exchange, symbol)
        if gegenstellen_zeilen is None:
            raise RuntimeError(f"Offene Stop-Order für {symbol} nicht lesbar")
        gegenstelle_gesamt += len(gegenstellen_zeilen)
        for gz in gegenstellen_zeilen:
            gesehene_algo_ids.add(gz.algo_id)
            eigene_zeile = eigene_nach_algo_id.get(gz.algo_id)
            if eigene_zeile is None:
                if gz.algo_id in nachfuehrung_algo_ids:
                    # Entwurf 10.3, Punkt 6: während der laufenden Nachführung ist
                    # sowohl der alte als auch der neue Stop ein gültiger Zustand --
                    # kein Waisen-Alarm für genau dieses Fenster.
                    continue
                waisen.append(gz)
                continue
            if eigene_zeile.side != gz.side:
                triggerabweichung.append((eigene_zeile, gz))
                continue
            trigger_bekannt = eigene_zeile.trigger_bestaetigt is not None
            if trigger_bekannt and eigene_zeile.trigger_bestaetigt != gz.trigger_kurs:
                triggerabweichung.append((eigene_zeile, gz))
                continue
            soll = eigene_zeile.menge_bestaetigt or eigene_zeile.menge_soll
            if soll > 0 and abs(gz.menge - soll) / soll > mengen_rundungstoleranz:
                mengenabweichung.append((eigene_zeile, gz))
                continue
            if eigene_zeile.menge_bestaetigt is None:
                eigene_zeile.menge_bestaetigt = gz.menge
            if eigene_zeile.trigger_bestaetigt is None:
                eigene_zeile.trigger_bestaetigt = gz.trigger_kurs

    for zeile in eigene_offene:
        if zeile.gegenstellen_algo_id not in gesehene_algo_ids:
            fehlend.append(zeile)

    ergebnis = "bestanden"
    if fehlend or waisen or mengenabweichung or triggerabweichung:
        ergebnis = "abweichung"

    session.add(
        StopAbgleichLauf(
            begonnen_at=begonnen_at,
            beendet_at=utcnow(),
            anzahl_eigene_offene=len(eigene_offene),
            anzahl_gegenstelle_offene=gegenstelle_gesamt,
            anzahl_fehlend=len(fehlend),
            anzahl_waisen=len(waisen),
            anzahl_mengenabweichung=len(mengenabweichung),
            anzahl_triggerabweichung=len(triggerabweichung),
            ergebnis=ergebnis,
        )
    )

    if ergebnis == "abweichung":
        await setze_firmenweit(
            session,
            ausloeser=_ABWEICHUNG_AUSLOESER,
            begruendung=(
                f"Täglicher Stop-Orderbuch-Abgleich (B-12 Punkt 2): "
                f"{len(fehlend)} fehlend, {len(waisen)} Waisen, "
                f"{len(mengenabweichung)} Mengenabweichungen, "
                f"{len(triggerabweichung)} Triggerabweichungen."
            ),
        )

    await session.commit()
    return ergebnis == "bestanden"


async def _pruefe_totmannschalter(session_factory, totmannfrist: timedelta) -> bool:
    """Entwurf Abschnitt 10.6, Punkt 2: Durchlauf bleibt aus -- eigener, von
    ``_fuehre_abgleich_durch`` unabhängiger Blick auf den letzten bestandenen Lauf.
    Läuft nach jedem Durchlauf UND wird beim Prozessstart einmal zusätzlich
    aufgerufen (siehe ``app.main``), damit ein langer Stillstand auch beim Neustart
    erkannt wird, nicht erst nach dem nächsten vollen Intervall.

    Gibt zurück, ob dieser Aufruf das Kennzeichen gesetzt hat (Pfad 2, HED-191
    Abschnitt 1b) -- der Aufrufer löst dann den Storno-Sweep aus."""
    async with session_factory() as session:
        letzter_bestandener = await session.scalar(
            select(StopAbgleichLauf.beendet_at)
            .where(StopAbgleichLauf.ergebnis == "bestanden")
            .order_by(StopAbgleichLauf.beendet_at.desc())
            .limit(1)
        )
        if letzter_bestandener is not None and utcnow() - letzter_bestandener <= totmannfrist:
            return False
        # Kein bestandener Lauf überhaupt (frischer Start, E-9: auch das zählt als
        # Ausbleiben) ODER der letzte liegt zu lange zurück.
        await setze_firmenweit(
            session,
            ausloeser=_FEHLGESCHLAGEN_AUSLOESER,
            begruendung=(
                "Totmannschalter (B-12, Entwurf Abschnitt 10.6): letzter bestandener "
                f"Stop-Orderbuch-Abgleich liegt länger als {totmannfrist} zurück "
                f"(zuletzt: {letzter_bestandener})."
            ),
        )
        await session.commit()
        return True

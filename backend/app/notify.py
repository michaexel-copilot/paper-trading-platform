"""Board-Benachrichtigung je Vorschlag in Z3 (HED-43, Entwurf Abschnitt 5.3).

Kanal: E-Mail an die Board-Verteilerliste, per smtplib (Python-Standardbibliothek,
siehe Prüfkarte-Hinweis im Plan-Dokument -- kein Drittanbieter-Versanddienst). Das
Dashboard bleibt Quelle der Wahrheit; die Mail selbst erlaubt keine Aktion.

Fehlerverhalten (Entwurf 5.3): ein Versandfehler wird protokolliert und einmal
wiederholt. Bleibt der zweite Versuch auch erfolglos, gilt der Ausfall als dauerhaft --
das löst das Warnbanner im Dashboard (``status`` bleibt/wird ``failed``) und eine
Einzelmeldung an den CRO aus. Keiner der beiden Fälle verlängert die Verfallsfrist oder
verhindert die Vorschlagsentstehung; die 2-Stunden-Frist läuft ab Dashboard-Anzeige
(``Proposal.displayed_at`` / ``expires_at``), nicht ab E-Mail-Zustellung.
"""

import asyncio
import logging
import smtplib
from email.message import EmailMessage

from sqlalchemy import select

from app.config import Settings
from app.db import SessionFactory
from app.dbtypes import utcnow
from app.models import BoardNotification, Proposal

log = logging.getLogger(__name__)


class NotifySendError(Exception):
    pass


def _compose(settings: Settings, proposal: Proposal) -> EmailMessage:
    link = f"{settings.dashboard_base_url.rstrip('/')}/dashboard/proposals/{proposal.id}"
    msg = EmailMessage()
    msg["Subject"] = f"Neuer Handelsvorschlag wartet auf Entscheidung: {proposal.instrument}"
    msg["From"] = settings.smtp_from
    msg["To"] = ", ".join(settings.board_notify_email_list())
    msg.set_content(
        "Ein neuer Handelsvorschlag wartet auf eine Board-Entscheidung.\n\n"
        f"Strategie: {proposal.strategy_id}\n"
        f"Instrument: {proposal.instrument}\n"
        f"Richtung: {proposal.direction}\n"
        f"Größe: {proposal.size}\n"
        f"Vorschlagskurs: {proposal.proposal_price}\n"
        f"Stop-Kurs: {proposal.stop_price}\n"
        f"Risiko je Trade (EUR): {proposal.risk_eur}\n"
        f"Summenrisiko-Auslastung nach Trade: {proposal.aggregate_risk_utilization_after}\n"
        f"Verfällt um (UTC): "
        f"{proposal.expires_at.isoformat() if proposal.expires_at else 'unbekannt'}\n\n"
        f"Entscheidung ausschließlich im Dashboard: {link}\n"
        "Diese E-Mail selbst löst keine Aktion aus.\n"
    )
    return msg


def _send_blocking(settings: Settings, msg: EmailMessage) -> None:
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as client:
        if settings.smtp_starttls:
            client.starttls()
        if settings.smtp_user:
            client.login(settings.smtp_user, settings.smtp_password or "")
        client.send_message(msg)


def _compose_cro_alert(settings: Settings, proposal: Proposal) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"Board-Benachrichtigung dauerhaft fehlgeschlagen -- Vorschlag {proposal.id}"
    msg["From"] = settings.smtp_from
    msg["To"] = settings.cro_alert_email
    msg.set_content(
        f"Die Board-Benachrichtigung für Vorschlag {proposal.id} ({proposal.instrument}) "
        "ist nach einer Wiederholung weiterhin fehlgeschlagen. Der Vorschlag selbst steht "
        "weiterhin im Dashboard und läuft nach seiner regulären Frist ab, unabhängig vom "
        "E-Mail-Versand.\n"
    )
    return msg


async def notify_new_proposal(
    session_factory: SessionFactory, settings: Settings, proposal_id: int
) -> None:
    """Einmaliger Versandversuch mit genau einer Wiederholung bei Fehler.

    Wird vom Hintergrundlauf in ``app.main`` für jeden Vorschlag aufgerufen, der neu in
    Z3 steht und noch keine ``BoardNotification``-Zeile hat (Zeile wird hier zuerst
    angelegt -- das verhindert einen Doppelversand bei zwei sich überlappenden
    Durchläufen des Pollers, siehe unique-Constraint auf ``proposal_id``).
    """
    async with session_factory() as session:
        proposal = await session.get(Proposal, proposal_id)
        if proposal is None:
            return
        record = BoardNotification(proposal_id=proposal_id)
        session.add(record)
        try:
            await session.commit()
        except Exception:
            # Eine andere Durchlauf-Instanz war schneller -- kein zweiter Versand.
            await session.rollback()
            return

        msg = _compose(settings, proposal)
        last_error: str | None = None
        sent = False
        for _attempt in range(2):  # Erstversuch + eine Wiederholung
            record.attempts += 1
            try:
                await asyncio.to_thread(_send_blocking, settings, msg)
                sent = True
                break
            except Exception as exc:  # smtplib wirft diverse Exception-Typen
                last_error = str(exc)
                log.warning(
                    "Board-Benachrichtigung für Vorschlag %s fehlgeschlagen: %s", proposal_id, exc
                )

        if sent:
            record.status = "sent"
            record.sent_at = utcnow()
        else:
            record.status = "failed"
            record.last_error = last_error
            if settings.cro_alert_email and not record.cro_alert_sent:
                try:
                    await asyncio.to_thread(
                        _send_blocking, settings, _compose_cro_alert(settings, proposal)
                    )
                    record.cro_alert_sent = True
                except Exception as exc:
                    log.error(
                        "CRO-Einzelmeldung für Vorschlag %s ebenfalls fehlgeschlagen: %s",
                        proposal_id,
                        exc,
                    )
        await session.commit()


async def pending_notification_count(session_factory: SessionFactory) -> int:
    """Anzahl dauerhaft fehlgeschlagener Benachrichtigungen -- Grundlage für das
    Warnbanner im Dashboard (Entwurf 5.3, Fehlerverhalten)."""
    async with session_factory() as session:
        rows = await session.scalars(
            select(BoardNotification).where(BoardNotification.status == "failed")
        )
        return len(list(rows))

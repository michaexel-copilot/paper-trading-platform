from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.dbtypes import Money, UtcDateTime, utcnow
from app.models.base import Base

PROPOSAL_MODES = ("paper", "live")
PROPOSAL_DIRECTIONS = ("long", "short")
PROPOSAL_KINDS = ("entry", "increase")
STAGE1_RESULTS = ("passed", "rejected")
BOARD_ACTIONS = ("approve", "change", "discard")
LOG_CATEGORIES = ("stage1_rejection", "stage2_rejection", "board_decision")

# Zustandscodes, Entwurf HED-36 Fassung 1.1 Abschnitt 4, Tabelle 4.2 -- wörtlich übernommen
# für Auditierbarkeit (Protokoll, Dashboard und Entwurfstext sprechen dieselbe Sprache).
STATE_ENTSTANDEN = "entstanden"  # Z0
STATE_STUFE1_LAEUFT = "stufe1_laeuft"  # Z1
STATE_STUFE1_ABGELEHNT = "stufe1_abgelehnt"  # Z2, terminal
STATE_WARTET_AUF_BOARD = "wartet_auf_board"  # Z3
STATE_VERFALLEN = "verfallen"  # Z4, terminal
STATE_BOARD_VERWORFEN = "board_verworfen"  # Z5, terminal
STATE_BOARD_GEAENDERT = "board_geaendert"  # Z6, terminal for this record
STATE_BOARD_FREIGEGEBEN = "board_freigegeben"  # Z7
STATE_STUFE2_LAEUFT = "stufe2_laeuft"  # Z8
STATE_STUFE2_ABGELEHNT = "stufe2_abgelehnt"  # Z9, terminal
STATE_VERSANDT = "versandt"  # Z10
STATE_GEFUELLT = "gefuellt"  # Z11, terminal
STATE_TEILGEFUELLT_BEENDET = "teilgefuellt_beendet"  # Z12, terminal
STATE_VON_GEGENSTELLE_ABGELEHNT = "von_gegenstelle_abgelehnt"  # Z13, terminal

# Z0 allows both Z1 and Z2 directly (e.g. the sperrkennzeichen check before stage 1 can
# reject without ever entering stufe1_laeuft); Z1 separately allows both Z2 and Z3. Both
# rows come straight from table 4.2, not a derived shortcut.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STATE_ENTSTANDEN: frozenset({STATE_STUFE1_LAEUFT, STATE_STUFE1_ABGELEHNT}),
    STATE_STUFE1_LAEUFT: frozenset({STATE_STUFE1_ABGELEHNT, STATE_WARTET_AUF_BOARD}),
    STATE_WARTET_AUF_BOARD: frozenset(
        {STATE_VERFALLEN, STATE_BOARD_VERWORFEN, STATE_BOARD_GEAENDERT, STATE_BOARD_FREIGEGEBEN}
    ),
    STATE_BOARD_FREIGEGEBEN: frozenset({STATE_STUFE2_LAEUFT}),
    STATE_STUFE2_LAEUFT: frozenset({STATE_STUFE2_ABGELEHNT, STATE_VERSANDT}),
    STATE_VERSANDT: frozenset(
        {STATE_GEFUELLT, STATE_TEILGEFUELLT_BEENDET, STATE_VON_GEGENSTELLE_ABGELEHNT}
    ),
}
# Terminal states (Z2, Z4, Z5, Z6, Z9, Z11, Z12, Z13) have no entry above, so
# ALLOWED_TRANSITIONS.get(state, frozenset()) correctly yields no allowed follow-up state.


class Proposal(Base):
    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Identität / Herkunft (Entwurf 4.1)
    mode: Mapped[str] = mapped_column(String(4))  # paper | live
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), index=True)
    # Einmalig bei Signalentstehung aus Strategy.gate_status kopiert, danach unveränderlich
    # (Festlegung 4.4.6). Erzwungen in _freeze_strategy_gate_status unten.
    strategy_gate_status: Mapped[str] = mapped_column(String(2))
    signal_id: Mapped[str] = mapped_column(String(64), index=True)
    parent_proposal_id: Mapped[int | None] = mapped_column(
        ForeignKey("proposals.id", ondelete="SET NULL")
    )
    change_generation: Mapped[int] = mapped_column(default=0)

    # Handelsinhalt: Snapshot zum Entstehungszeitpunkt, danach unveränderlich.
    instrument: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(5))  # long | short
    size: Mapped[Decimal] = mapped_column(Money)
    proposal_price: Mapped[Decimal] = mapped_column(Money)
    stop_price: Mapped[Decimal] = mapped_column(Money)
    stop_distance: Mapped[Decimal] = mapped_column(Money)
    stop_distance_pct: Mapped[Decimal] = mapped_column(Money)
    proposal_kind: Mapped[str] = mapped_column(String(10))  # entry | increase

    # Stufe-1-Kennzahlen: eingefroren beim Z0->Z1->Z2/Z3-Übergang, danach unveränderlich.
    nav: Mapped[Decimal | None] = mapped_column(Money)
    drawdown: Mapped[Decimal | None] = mapped_column(Money)
    aggregate_risk_utilization_before: Mapped[Decimal | None] = mapped_column(Money)
    aggregate_risk_utilization_after: Mapped[Decimal | None] = mapped_column(Money)
    risk_eur: Mapped[Decimal | None] = mapped_column(Money)
    correlation_to_open_positions: Mapped[Decimal | None] = mapped_column(Money)
    price_age_seconds: Mapped[int | None] = mapped_column()
    limit_utilization: Mapped[list | None] = mapped_column(JSON)
    stage1_result: Mapped[str | None] = mapped_column(String(10))  # passed | rejected
    stage1_reason: Mapped[str | None] = mapped_column(String(500))

    # Zeitstempel: nullable bis zum jeweiligen Ereignis, danach unveränderlich.
    signal_at: Mapped[datetime] = mapped_column(UtcDateTime)
    stage1_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    displayed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    stage2_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    filled_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    # Entscheidung / Ergebnis.
    state: Mapped[str] = mapped_column(String(30), default=STATE_ENTSTANDEN, index=True)
    decided_by: Mapped[str | None] = mapped_column(String(100))  # Kennung oder "system"
    board_action: Mapped[str | None] = mapped_column(String(10))  # approve | change | discard
    log_category: Mapped[str | None] = mapped_column(String(30))
    rejection_reason_code: Mapped[str | None] = mapped_column(String(50))
    rejection_reason_text: Mapped[str | None] = mapped_column(String(500))
    approval_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    limit_price: Mapped[Decimal | None] = mapped_column(Money)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    @validates("strategy_gate_status")
    def _freeze_strategy_gate_status(self, _key: str, value: str) -> str:
        current = self.__dict__.get("strategy_gate_status")
        if current is not None and value != current:
            raise ValueError(
                "strategy_gate_status ist nach Anlage unveränderlich (Festlegung 4.4.6)"
            )
        return value


class ProposalEvent(Base):
    """Ein unveränderlicher Protokolleintrag je Zustandsübergang (Entwurf 4.4.5/4.4.8).

    Zeilen werden eingefügt und nie aktualisiert.
    """

    __tablename__ = "proposal_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("proposals.id", ondelete="CASCADE"), index=True
    )
    from_state: Mapped[str | None] = mapped_column(String(30))
    to_state: Mapped[str] = mapped_column(String(30))
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    actor: Mapped[str] = mapped_column(String(100))  # "system" oder Kennung
    reason: Mapped[str | None] = mapped_column(String(500))
    log_category: Mapped[str | None] = mapped_column(String(30))

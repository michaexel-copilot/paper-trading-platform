from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

GATE_STATUSES = ("S1", "S2", "S3")


class Strategy(Base):
    """Minimal record: only what the proposal gate (app.trading.proposals) needs to read.

    Gate assignment is a separate process, outside this model and outside this ticket
    (HED-41 / HED-36 Entwurf Abschnitt 4).
    """

    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(primary_key=True)
    gate_status: Mapped[str] = mapped_column(String(2), default="S1", index=True)

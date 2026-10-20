"""B-16: Buch-Sicherung-Lauf (HED-85, Plan Revision 4)

Revision ID: a1fc9343b245
Revises: 1f5a48e77b99
Create Date: 2026-10-20 10:29:53.399883

"""

from collections.abc import Sequence

import sqlalchemy as sa

import app.dbtypes
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1fc9343b245"
down_revision: str | Sequence[str] | None = "1f5a48e77b99"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "buch_sicherung_lauf",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("begonnen_at", app.dbtypes.UtcDateTime(timezone=True), nullable=False),
        sa.Column("beendet_at", app.dbtypes.UtcDateTime(timezone=True), nullable=True),
        sa.Column("ziel_remote", sa.String(length=80), nullable=False),
        sa.Column("dateiname", sa.String(length=120), nullable=True),
        sa.Column("bytes_gesichert", sa.BigInteger(), nullable=True),
        sa.Column("ergebnis", sa.String(length=15), nullable=False),
        sa.Column("fehlertext", sa.String(length=500), nullable=True),
        sa.Column("cro_alert_gesendet", sa.Boolean(), nullable=False),
        sa.Column("betriebsart", sa.String(length=5), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_buch_sicherung_lauf")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("buch_sicherung_lauf")

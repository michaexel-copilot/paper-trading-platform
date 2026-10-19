"""Pfad 7: Fristeskalation (B-15, HED-185, CRO-Entscheidung HED-191 Abschnitt 2)

Revision ID: 1f5a48e77b99
Revises: c3e1f9a04d2b
Create Date: 2026-10-19 12:59:23.891932

"""

from collections.abc import Sequence

import sqlalchemy as sa

import app.dbtypes
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1f5a48e77b99"
down_revision: str | Sequence[str] | None = "c3e1f9a04d2b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "frist_eskalation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kennzeichen_typ", sa.String(length=10), nullable=False),
        sa.Column("strategie_schluessel", sa.String(length=64), nullable=True),
        sa.Column("frist_typ", sa.String(length=30), nullable=False),
        sa.Column("ausloese_zeitpunkt", app.dbtypes.UtcDateTime(timezone=True), nullable=False),
        sa.Column("befund", sa.String(length=500), nullable=False),
        sa.Column("occurred_at", app.dbtypes.UtcDateTime(timezone=True), nullable=False),
        sa.Column("betriebsart", sa.String(length=5), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_frist_eskalation")),
        sa.UniqueConstraint(
            "kennzeichen_typ", "strategie_schluessel", "ausloese_zeitpunkt",
            name="uq_frist_eskalation_trigger",
        ),
    )
    with op.batch_alter_table("frist_eskalation", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_frist_eskalation_strategie_schluessel"),
            ["strategie_schluessel"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("frist_eskalation", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_frist_eskalation_strategie_schluessel"))

    op.drop_table("frist_eskalation")

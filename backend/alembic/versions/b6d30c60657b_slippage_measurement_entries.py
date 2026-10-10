"""slippage measurement entries

Revision ID: b6d30c60657b
Revises: c6d95812fcb8
Create Date: 2026-10-10 17:22:52.883498

"""

from collections.abc import Sequence

import sqlalchemy as sa

import app.dbtypes
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b6d30c60657b"
down_revision: str | Sequence[str] | None = "c6d95812fcb8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "slippage_messung_eintrag",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("proposal_id", sa.Integer(), nullable=False),
        sa.Column("dauer_signal_bis_freigabe_sekunden", sa.Integer(), nullable=False),
        sa.Column(
            "kursveraenderung_signal_bis_freigabe",
            app.dbtypes.Money(precision=38, scale=18),
            nullable=True,
        ),
        sa.Column("dauer_freigabe_bis_versand_sekunden", sa.Integer(), nullable=False),
        sa.Column("dauer_versand_bis_fuellung_sekunden", sa.Integer(), nullable=False),
        sa.Column("dauer_freigabe_bis_fuellung_sekunden", sa.Integer(), nullable=False),
        sa.Column("fuellpreis", app.dbtypes.Money(precision=38, scale=18), nullable=False),
        sa.Column("referenzkurs", app.dbtypes.Money(precision=38, scale=18), nullable=False),
        sa.Column(
            "preisabweichung_absolut", app.dbtypes.Money(precision=38, scale=18), nullable=False
        ),
        sa.Column(
            "preisabweichung_prozentpunkte",
            app.dbtypes.Money(precision=38, scale=18),
            nullable=False,
        ),
        sa.Column(
            "preisabweichung_risikoeinheiten",
            app.dbtypes.Money(precision=38, scale=18),
            nullable=False,
        ),
        sa.Column("occurred_at", app.dbtypes.UtcDateTime(timezone=True), nullable=False),
        sa.Column("betriebsart", sa.String(length=5), nullable=False),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["proposals.id"],
            name=op.f("fk_slippage_messung_eintrag_proposal_id_proposals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_slippage_messung_eintrag")),
    )
    with op.batch_alter_table("slippage_messung_eintrag", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_slippage_messung_eintrag_proposal_id"), ["proposal_id"], unique=True
        )


def downgrade() -> None:
    with op.batch_alter_table("slippage_messung_eintrag", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_slippage_messung_eintrag_proposal_id"))

    op.drop_table("slippage_messung_eintrag")

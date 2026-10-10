"""platform separation: installation marker, sperrkennzeichen, versand engpass log

Revision ID: c6d95812fcb8
Revises: 2f0c7ef858fa
Create Date: 2026-10-10 15:56:51.833240

"""

from collections.abc import Sequence

import sqlalchemy as sa

import app.dbtypes
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c6d95812fcb8"
down_revision: str | Sequence[str] | None = "6ee666ffb666"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "firmenweites_sperrkennzeichen",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("gesetzt", sa.Boolean(), nullable=False),
        sa.Column("updated_at", app.dbtypes.UtcDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_firmenweites_sperrkennzeichen")),
    )
    op.create_table(
        "installation_marker",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("betriebsart", sa.String(length=5), nullable=False),
        sa.Column("artefakt_art", sa.String(length=14), nullable=False),
        sa.Column("created_at", app.dbtypes.UtcDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_installation_marker")),
    )
    op.create_table(
        "strategie_sperrkennzeichen",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("strategie_schluessel", sa.String(length=64), nullable=False),
        sa.Column("gesetzt", sa.Boolean(), nullable=False),
        sa.Column("updated_at", app.dbtypes.UtcDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_strategie_sperrkennzeichen")),
        sa.UniqueConstraint(
            "strategie_schluessel", name=op.f("uq_strategie_sperrkennzeichen_strategie_schluessel")
        ),
    )
    op.create_table(
        "versand_engpass_eintrag",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("gattung", sa.String(length=20), nullable=False),
        sa.Column("strategie_schluessel", sa.String(length=64), nullable=False),
        sa.Column("exchange", sa.String(length=20), nullable=False),
        sa.Column("angenommen", sa.Boolean(), nullable=False),
        sa.Column("ablehnungsgrund", sa.String(length=200), nullable=True),
        sa.Column("order_referenz", sa.JSON(), nullable=False),
        sa.Column("gegenstellen_order_id", sa.String(length=100), nullable=True),
        sa.Column("occurred_at", app.dbtypes.UtcDateTime(timezone=True), nullable=False),
        sa.Column("betriebsart", sa.String(length=5), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_versand_engpass_eintrag")),
    )
    with op.batch_alter_table("versand_engpass_eintrag", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_versand_engpass_eintrag_gattung"), ["gattung"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_versand_engpass_eintrag_strategie_schluessel"),
            ["strategie_schluessel"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("versand_engpass_eintrag", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_versand_engpass_eintrag_strategie_schluessel"))
        batch_op.drop_index(batch_op.f("ix_versand_engpass_eintrag_gattung"))

    op.drop_table("versand_engpass_eintrag")
    op.drop_table("strategie_sperrkennzeichen")
    op.drop_table("installation_marker")
    op.drop_table("firmenweites_sperrkennzeichen")

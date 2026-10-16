"""B-12: Stop-Orderbuch, Abgleichs-Protokoll, neue Proposal-Felder

HED-84, Entwurf Abschnitt 6.3/10.2.

Revision ID: a1b2c3d4e5f6
Revises: f3a9c1d4e7b2
Create Date: 2026-10-14 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

import app.dbtypes
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "f3a9c1d4e7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("proposals", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("gegenstellen_order_id", sa.String(length=100), nullable=True)
        )
        batch_op.add_column(sa.Column("gefuellte_menge", app.dbtypes.Money(), nullable=True))

    op.create_table(
        "stop_order_buch",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("betriebsart", sa.String(length=5), nullable=False),
        sa.Column("strategie_schluessel", sa.String(length=64), nullable=False),
        sa.Column("exchange", sa.String(length=20), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=5), nullable=False),
        sa.Column("menge_soll", app.dbtypes.Money(), nullable=False),
        sa.Column("menge_bestaetigt", app.dbtypes.Money(), nullable=True),
        sa.Column("trigger_soll", app.dbtypes.Money(), nullable=False),
        sa.Column("trigger_bestaetigt", app.dbtypes.Money(), nullable=True),
        sa.Column("gegenstellen_algo_id", sa.String(length=100), nullable=True),
        sa.Column("anlass_proposal_id", sa.Integer(), nullable=True),
        sa.Column("versand_eintrag_id", sa.Integer(), nullable=True),
        sa.Column("zustand", sa.String(length=20), nullable=False),
        sa.Column("ersetzt_durch_id", sa.Integer(), nullable=True),
        sa.Column("erstellt_at", app.dbtypes.UtcDateTime(timezone=True), nullable=False),
        sa.Column("beendet_at", app.dbtypes.UtcDateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stop_order_buch")),
    )
    with op.batch_alter_table("stop_order_buch", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_stop_order_buch_strategie_schluessel"),
            ["strategie_schluessel"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_stop_order_buch_symbol"), ["symbol"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_stop_order_buch_gegenstellen_algo_id"),
            ["gegenstellen_algo_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_stop_order_buch_zustand"), ["zustand"], unique=False
        )

    op.create_table(
        "stop_abgleich_lauf",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("betriebsart", sa.String(length=5), nullable=False),
        sa.Column("begonnen_at", app.dbtypes.UtcDateTime(timezone=True), nullable=False),
        sa.Column("beendet_at", app.dbtypes.UtcDateTime(timezone=True), nullable=True),
        sa.Column("anzahl_eigene_offene", sa.Integer(), nullable=False),
        sa.Column("anzahl_gegenstelle_offene", sa.Integer(), nullable=False),
        sa.Column("anzahl_fehlend", sa.Integer(), nullable=False),
        sa.Column("anzahl_waisen", sa.Integer(), nullable=False),
        sa.Column("anzahl_mengenabweichung", sa.Integer(), nullable=False),
        sa.Column("anzahl_triggerabweichung", sa.Integer(), nullable=False),
        sa.Column("ergebnis", sa.String(length=15), nullable=False),
        sa.Column("fehlertext", sa.String(length=500), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stop_abgleich_lauf")),
    )


def downgrade() -> None:
    op.drop_table("stop_abgleich_lauf")

    with op.batch_alter_table("stop_order_buch", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_stop_order_buch_zustand"))
        batch_op.drop_index(batch_op.f("ix_stop_order_buch_gegenstellen_algo_id"))
        batch_op.drop_index(batch_op.f("ix_stop_order_buch_symbol"))
        batch_op.drop_index(batch_op.f("ix_stop_order_buch_strategie_schluessel"))

    op.drop_table("stop_order_buch")

    with op.batch_alter_table("proposals", schema=None) as batch_op:
        batch_op.drop_column("gefuellte_menge")
        batch_op.drop_column("gegenstellen_order_id")

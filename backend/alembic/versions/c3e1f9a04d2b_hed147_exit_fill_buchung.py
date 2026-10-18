"""HED-147: Exit-Fill-Buchung -- Delta-Tracking-Spalten für StopOrderBuch und
AutoExitLogEntry

HED-147, Entwurf Abschnitt 4.3, CRO-Abnahme HED-148/HED-150.

down_revision zeigt auf die Merge-Revision 858dd4e03635 (HED-153 Auflage 1,
B-11/B-12-Koepfe zusammengefuehrt), nicht mehr direkt auf a1b2c3d4e5f6 --
sonst entstuende durch diese Revision erneut ein zweiter Kopf neben
858dd4e03635.

Revision ID: c3e1f9a04d2b
Revises: 858dd4e03635
Create Date: 2026-10-16 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

import app.dbtypes
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3e1f9a04d2b"
down_revision: str | Sequence[str] | None = "858dd4e03635"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("stop_order_buch", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "gefuellte_menge_gebucht",
                app.dbtypes.Money(),
                nullable=True,
                server_default="0",
            )
        )

    with op.batch_alter_table("auto_exit_log_entries", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("gegenstellen_order_id", sa.String(length=100), nullable=True)
        )
        batch_op.add_column(sa.Column("exchange", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("boersensymbol", sa.String(length=32), nullable=True))
        batch_op.add_column(
            sa.Column("gefuellte_menge_gebucht", app.dbtypes.Money(), nullable=True)
        )
        batch_op.create_index(
            batch_op.f("ix_auto_exit_log_entries_gegenstellen_order_id"),
            ["gegenstellen_order_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("auto_exit_log_entries", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_auto_exit_log_entries_gegenstellen_order_id"))
        batch_op.drop_column("gefuellte_menge_gebucht")
        batch_op.drop_column("boersensymbol")
        batch_op.drop_column("exchange")
        batch_op.drop_column("gegenstellen_order_id")

    with op.batch_alter_table("stop_order_buch", schema=None) as batch_op:
        batch_op.drop_column("gefuellte_menge_gebucht")

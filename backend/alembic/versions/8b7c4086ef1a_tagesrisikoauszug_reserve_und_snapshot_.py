"""tagesrisikoauszug: externe reserve und snapshot-aufschluesselung

HED-83 (B-11): Mindest-Barbestand braucht die Reserve außerhalb der Börse als
Eingabegröße (``portfolios.external_reserve_cash``, default 0 -- im Papierbetrieb
gibt es keine echte externe Bankreserve). Die Tages-P&L-Aufteilung in
Handelsergebnis/Währungseffekt braucht je Tagesschnappschuss eine
Positionsaufschlüsselung (``value_snapshots.breakdown``), nicht nur die NAV-Summe.

Revision ID: 8b7c4086ef1a
Revises: f3a9c1d4e7b2
Create Date: 2026-10-14 06:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

import app.dbtypes
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8b7c4086ef1a"
down_revision: str | Sequence[str] | None = "f3a9c1d4e7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("portfolios", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "external_reserve_cash", app.dbtypes.Money(), nullable=False,
                server_default="0",
            )
        )

    with op.batch_alter_table("value_snapshots", schema=None) as batch_op:
        batch_op.add_column(sa.Column("breakdown", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("value_snapshots", schema=None) as batch_op:
        batch_op.drop_column("breakdown")

    with op.batch_alter_table("portfolios", schema=None) as batch_op:
        batch_op.drop_column("external_reserve_cash")

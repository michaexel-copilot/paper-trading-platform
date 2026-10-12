"""kill switch lifecycle protocol and suppressed proposal log

Revision ID: 88ed15da8f4c
Revises: b6d30c60657b
Create Date: 2026-10-11 10:46:59.040482

"""

from collections.abc import Sequence

import sqlalchemy as sa

import app.dbtypes
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "88ed15da8f4c"
down_revision: str | Sequence[str] | None = "b6d30c60657b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sperrkennzeichen_protokoll",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kennzeichen_typ", sa.String(length=10), nullable=False),
        sa.Column("strategie_schluessel", sa.String(length=64), nullable=True),
        sa.Column("aktion", sa.String(length=10), nullable=False),
        sa.Column("ausloeser", sa.String(length=50), nullable=False),
        sa.Column("entscheider_1", sa.String(length=100), nullable=True),
        sa.Column("entscheider_2", sa.String(length=100), nullable=True),
        sa.Column("begruendung", sa.String(length=1000), nullable=False),
        sa.Column("occurred_at", app.dbtypes.UtcDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sperrkennzeichen_protokoll")),
    )
    op.create_table(
        "vorschlag_unterdrueckt",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("strategy_id", sa.Integer(), nullable=False),
        sa.Column("signal_id", sa.String(length=64), nullable=False),
        sa.Column("grund", sa.String(length=50), nullable=False),
        sa.Column("occurred_at", app.dbtypes.UtcDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vorschlag_unterdrueckt")),
    )
    with op.batch_alter_table("sperrkennzeichen_protokoll", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_sperrkennzeichen_protokoll_strategie_schluessel"),
            ["strategie_schluessel"],
            unique=False,
        )
    with op.batch_alter_table("vorschlag_unterdrueckt", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_vorschlag_unterdrueckt_signal_id"), ["signal_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_vorschlag_unterdrueckt_strategy_id"), ["strategy_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("vorschlag_unterdrueckt", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_vorschlag_unterdrueckt_strategy_id"))
        batch_op.drop_index(batch_op.f("ix_vorschlag_unterdrueckt_signal_id"))
    with op.batch_alter_table("sperrkennzeichen_protokoll", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sperrkennzeichen_protokoll_strategie_schluessel"))

    op.drop_table("vorschlag_unterdrueckt")
    op.drop_table("sperrkennzeichen_protokoll")

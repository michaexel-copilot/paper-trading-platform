"""dashboard: optimistic lock on proposals, board notifications, action attempts

HED-43, Entwurf HED-36 Fassung 1.1 Abschnitt 5.

Revision ID: f3a9c1d4e7b2
Revises: 88ed15da8f4c
Create Date: 2026-10-11 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

import app.dbtypes
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a9c1d4e7b2"
down_revision: str | Sequence[str] | None = "88ed15da8f4c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Optimistic-lock-Zähler (Plan HED-43 Abschnitt 2): server_default='0' befüllt
    # bestehende Zeilen, Python-seitiger default=0 gilt für neue.
    with op.batch_alter_table("proposals", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("version", sa.Integer(), nullable=False, server_default="0")
        )

    op.create_table(
        "board_notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("proposal_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("created_at", app.dbtypes.UtcDateTime(timezone=True), nullable=False),
        sa.Column("sent_at", app.dbtypes.UtcDateTime(timezone=True), nullable=True),
        sa.Column("cro_alert_sent", sa.Boolean(), nullable=False),
        sa.Column("betriebsart", sa.String(length=5), nullable=False),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["proposals.id"],
            name=op.f("fk_board_notifications_proposal_id_proposals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_board_notifications")),
    )
    with op.batch_alter_table("board_notifications", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_board_notifications_proposal_id"), ["proposal_id"], unique=True
        )

    op.create_table(
        "proposal_action_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("proposal_id", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("action", sa.String(length=10), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("detail", sa.String(length=500), nullable=True),
        sa.Column("attempted_at", app.dbtypes.UtcDateTime(timezone=True), nullable=False),
        sa.Column("betriebsart", sa.String(length=5), nullable=False),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["proposals.id"],
            name=op.f("fk_proposal_action_attempts_proposal_id_proposals"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_proposal_action_attempts")),
    )
    with op.batch_alter_table("proposal_action_attempts", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_proposal_action_attempts_proposal_id"), ["proposal_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("proposal_action_attempts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_proposal_action_attempts_proposal_id"))

    op.drop_table("proposal_action_attempts")

    with op.batch_alter_table("board_notifications", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_board_notifications_proposal_id"))

    op.drop_table("board_notifications")

    with op.batch_alter_table("proposals", schema=None) as batch_op:
        batch_op.drop_column("version")

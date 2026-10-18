"""merge b11/b12 heads

HED-153 (CRO-Abnahme PR #35/HED-147, Auflage 1): HED-83 (B-11,
``8b7c4086ef1a``) und HED-84 (B-12, ``a1b2c3d4e5f6``) zweigen beide vom
selben Vorgaenger ``f3a9c1d4e7b2`` ab und wurden nie wieder zusammengefuehrt.
``app/main.py`` ruft beim Start ``alembic upgrade head`` auf -- mit zwei
Koepfen startet die Anwendung nicht. Reine Zusammenfuehrung, kein
Schema-Effekt.

Revision ID: 858dd4e03635
Revises: 8b7c4086ef1a, a1b2c3d4e5f6
Create Date: 2026-10-18 14:59:49.468491

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "858dd4e03635"
down_revision: str | Sequence[str] | None = ("8b7c4086ef1a", "a1b2c3d4e5f6")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""


def downgrade() -> None:
    """Downgrade schema."""

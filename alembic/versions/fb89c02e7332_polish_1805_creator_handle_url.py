"""polish_1805 — creator handle + url for author identity

Adds two nullable columns to the ``creators`` table so the portal can render
"by <name> @<handle>" on every skill page (polish_1805 item 4). Backfilled
by scripts/polish_1805_seed_creator_identity.py which seeds known internal
creators (WiseChef Team, Adam, Tori, Chef).

Both columns nullable — existing rows are unaffected and continue to render
as plain "by <name>" until the backfill is applied.

NOTE: revision id was changed from a1b2c3d4e5f6 → fb89c02e7332 to resolve
a collision with the existing add_referral_fields migration that already
owned a1b2c3d4e5f6 on the production branch. Functionally identical.

Revision ID: fb89c02e7332
Revises: e3f4a5b6c7d8
Create Date: 2026-05-17 19:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "fb89c02e7332"
down_revision: Union[str, None] = "e3f4a5b6c7d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add ``handle`` and ``url`` columns to ``creators``.

    Idempotent — if the columns already exist (e.g. partial prior apply via
    the colliding a1b2c3d4e5f6 head), skip the add silently. This keeps the
    upgrade safe to re-run on databases that may have been touched before
    the cycle was resolved.
    """
    from sqlalchemy import inspect
    bind = op.get_bind()
    insp = inspect(bind)
    existing = {c["name"] for c in insp.get_columns("creators")}
    if "handle" not in existing:
        op.add_column("creators", sa.Column("handle", sa.String(length=64), nullable=True))
    if "url" not in existing:
        op.add_column("creators", sa.Column("url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("creators", "url")
    op.drop_column("creators", "handle")

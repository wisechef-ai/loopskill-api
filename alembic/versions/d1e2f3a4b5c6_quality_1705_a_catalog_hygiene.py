"""quality_1705 Phase A — catalog hygiene foundations.

Adds the columns Phase A backfill writes into, but DOES NOT perform the
backfill itself (data migration runs out-of-band via
``scripts/quality_1705_catalog_backfill.py`` so it can be safely re-run,
audited, and rolled back).

Columns added:
  - ``skills.last_verified``   TIMESTAMP NULL — Phase A7 stamps every surviving
    skill to ``now()``; Phase C's last_verified cron updates it on each pass.
  - ``skills.archived_at``     TIMESTAMP NULL — was previously inferred from
    ``is_archived=true``; making it explicit lets Phase A record exactly when
    a skill was retired (provenance / un-archive audit).

Three previously-applied merge migrations on prod (``afca4ba836c2``,
``812c8f26fb4d``, ``b56c51c0ca29``) are committed back to the repo in this PR
so future ``alembic upgrade head`` from a clean checkout works. The merge
revisions themselves are no-ops; only the chain healing matters. Per
``executing-golazo-plan`` pitfall #4.

Revision ID: d1e2f3a4b5c6
Revises: cbd93b49a094
Create Date: 2026-05-17 18:30:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, Sequence[str], None] = "cbd93b49a094"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns("skills")}

    if "last_verified" not in existing_cols:
        op.add_column(
            "skills",
            sa.Column("last_verified", sa.DateTime(timezone=True), nullable=True),
        )
    if "archived_at" not in existing_cols:
        op.add_column(
            "skills",
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns("skills")}

    if "archived_at" in existing_cols:
        op.drop_column("skills", "archived_at")
    if "last_verified" in existing_cols:
        op.drop_column("skills", "last_verified")

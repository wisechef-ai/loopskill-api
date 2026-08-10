"""bundles0811 P3.6 — federation_hub_skills.license (recorded, never enforced)

Revision ID: bundles0811_p36_hub_license
Revises: mesh0408_w2_sub_event_at
Create Date: 2026-08-11

Adds a nullable ``license`` column to ``federation_hub_skills`` so a saved
filter (P3.6 gate 2: "filter the federated index by source + license") is a
real, DB-level capability. The live Hub snapshot (verified 2026-08-11, all
90,605 rows) does not populate a license field today — every existing row
stays NULL after this migration, and every filter that includes
``license=...`` degrades honestly to "no rows carry that license yet" rather
than inventing a value. This is additive-only and mirrors the pattern already
used for ``owner_handle`` (spotify_2607/0): the column exists so the reindex
cron or a future per-skill resolution can populate it with ZERO further
migration the moment any source starts shipping it.

Q3 (plan §0): licence is recorded, never enforced — no code path anywhere
gates or blocks on this column. It is a filter/display field only.

Nullable with no server default: instant ALTER on Postgres, no backfill
required (matches the ``mesh0408_w2_sub_event_at`` migration's pattern for
the identical "cheap additive nullable column" shape).

DOWNGRADE: drop the column. Non-destructive to any other data.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "bundles0811_p36_hub_license"
down_revision = "mesh0408_w2_sub_event_at"
branch_labels = None
depends_on = None

_TABLE = "federation_hub_skills"
_COLUMN = "license"


def upgrade() -> None:
    """Add the nullable ``license`` column. Idempotent: tolerates the column
    already existing (out-of-band merge migrations have accumulated on this
    table before — see spotify_2607_0_owner_handle.py's identical guard)."""
    bind = op.get_bind()
    existing = {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}
    if _COLUMN not in existing:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(64), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    existing = {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}
    if _COLUMN in existing:
        op.drop_column(_TABLE, _COLUMN)

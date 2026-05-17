"""quality_1705 Phase C — add quality_score column.

Per plan §3 Phase C step 6, the quality_score is a 0-10 float computed
from a weighted average of catalog hygiene signals. The migration just
adds the column; the actual computation lives in
``scripts/quality_1705_compute_quality_score.py`` and runs nightly via
the existing watchdog cron.

Revision ID: e3f4a5b6c7d8
Revises: d1e2f3a4b5c6
Create Date: 2026-05-17 20:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e3f4a5b6c7d8"
down_revision: Union[str, Sequence[str], None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns("skills")}
    if "quality_score" not in existing_cols:
        op.add_column(
            "skills",
            sa.Column("quality_score", sa.Float(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns("skills")}
    if "quality_score" in existing_cols:
        op.drop_column("skills", "quality_score")

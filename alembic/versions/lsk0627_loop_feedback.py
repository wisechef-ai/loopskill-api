"""loopskill_run_0627 — loop run/rating counters + loop_ratings table

Revision ID: lsk0627_loop_feedback
Revises: f1b2c3d4e5a6
Create Date: 2026-06-27 00:00:00.000000

Makes the loop registry feel ALIVE and gives it social proof (the stars + rating
axes). A vetted loop registry with every loop showing "0 runs, no rating" reads
as dead; these counters + ratings are the trust signal.

Adds to ``loops``:
  run_count     INT NOT NULL DEFAULT 0  — incremented on each verify run
  rating_count  INT NOT NULL DEFAULT 0  — number of ratings backing rating_avg

Creates ``loop_ratings``:
  id              UUID PK
  loop_id         FK → loops(id) ON DELETE CASCADE
  rater_user_id   UUID, nullable (anonymous/self-host ratings allowed)
  rating          INT CHECK (1..5)
  comment         TEXT, nullable
  created_at      TIMESTAMPTZ DEFAULT NOW()

Index: (loop_id) for fast per-loop aggregation. Postgres gets a partial unique
index so a known user can rate a given loop at most once (re-rating UPDATEs);
SQLite (tests/self-host) skips the partial unique index.

DOWNGRADE: drop loop_ratings, drop the two loops columns.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

# revision identifiers, used by Alembic.
revision = "lsk0627_loop_feedback"
down_revision = "f1b2c3d4e5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add loop run/rating counters and the loop_ratings table."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    uuid_type = PG_UUID(as_uuid=True) if is_postgres else sa.Text()

    # ── counters on loops ──
    op.add_column(
        "loops",
        sa.Column("run_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "loops",
        sa.Column("rating_count", sa.Integer(), nullable=False, server_default="0"),
    )

    # ── loop_ratings table ──
    op.create_table(
        "loop_ratings",
        sa.Column(
            "id",
            uuid_type,
            primary_key=True,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        sa.Column(
            "loop_id",
            uuid_type,
            sa.ForeignKey("loops.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rater_user_id", uuid_type, nullable=True),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            # CURRENT_TIMESTAMP is portable: Postgres maps it to now(), SQLite
            # fills it at INSERT. A Postgres-only NOW() leaves SQLite with no
            # default -> NOT NULL violation on the migrated cold-clone (caught by
            # the docker E2E, not by create_all-based unit tests).
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("rating >= 1 AND rating <= 5", name="ck_loop_rating_range"),
    )
    op.create_index("idx_loop_ratings_loop_id", "loop_ratings", ["loop_id"])

    # One rating per (loop, known user); re-rating UPDATEs the row. Partial unique
    # index is Postgres-only — SQLite test/self-host enforces uniqueness in code.
    if is_postgres:
        op.execute(
            """
            CREATE UNIQUE INDEX idx_loop_ratings_loop_user_unique
            ON loop_ratings(loop_id, rater_user_id)
            WHERE rater_user_id IS NOT NULL
            """
        )


def downgrade() -> None:
    """Drop loop_ratings and the two loops counter columns."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS idx_loop_ratings_loop_user_unique")
    op.drop_index("idx_loop_ratings_loop_id", table_name="loop_ratings")
    op.drop_table("loop_ratings")
    op.drop_column("loops", "rating_count")
    op.drop_column("loops", "run_count")

"""merge v7 phase E and J heads

Reconciles the two parallel migration heads created during the v7 sprint
(2026-05-06) when Phase E (pgvector recall) and Phase J (chef→maestro
rename + skill_aliases) both branched from b3c4d5e6f701 (Phase F taxonomy)
and were merged into main without a coordinating merge migration.

Both phases touch disjoint tables (skills.embedding column vs new
skill_aliases table), so this is a pure structural merge — no DDL needed.

Caught and fixed during the 2026-05-07 production cutover postmortem.

Revision ID: d8a4b1c2e9f3
Revises: c5d6e7f8a902, c7a8b9d0e1f2
Create Date: 2026-05-07
"""

from alembic import op  # noqa: F401


# revision identifiers, used by Alembic.
revision = "d8a4b1c2e9f3"
down_revision = ("c5d6e7f8a902", "c7a8b9d0e1f2")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No-op: structural merge of two disjoint migration heads."""
    pass


def downgrade() -> None:
    """No-op: structural merge of two disjoint migration heads."""
    pass

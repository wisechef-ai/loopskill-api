"""loopclose_3005 Phase J — per-cookbook feedback routing

Revision ID: lc3005_j_feedback_repo
Revises: lc3005_x_cookbook_owner_ck
Create Date: 2026-06-02 00:00:00.000000

Adds two nullable columns to the ``cookbooks`` table:

  feedback_repo  TEXT   — ``owner/repo`` string of the user's GitHub repo.
                          NULL  → fall back to the default wisechef-ai/recipes-api.
  feedback_mode  TEXT   — ``'pat'`` (PAT-based) or ``'github_app'`` (future App token).
                          NULL  → use the system default (wisechef PAT / _REPO).

Constraint: when feedback_repo IS NOT NULL, feedback_mode MUST also be set.
Both columns may be NULL simultaneously (no custom routing configured).

CHECK CONSTRAINT NOTE: SQLite supports CHECK constraints in CREATE TABLE but
NOT via ALTER TABLE ADD CONSTRAINT.  Since cookbooks already exists, the check
constraints are Postgres-only.  On SQLite (local dev), constraint semantics are
maintained by application-layer validation.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "lc3005_j_feedback_repo"
down_revision = "lc3005_x_cookbook_owner_ck"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add feedback_repo and feedback_mode to cookbooks."""
    is_pg = op.get_bind().dialect.name == "postgresql"

    op.add_column(
        "cookbooks",
        sa.Column("feedback_repo", sa.Text(), nullable=True),
    )
    op.add_column(
        "cookbooks",
        sa.Column("feedback_mode", sa.Text(), nullable=True),
    )

    # CHECK constraints: Postgres only — SQLite doesn't support ALTER TABLE
    # ADD CONSTRAINT.  op.create_check_constraint raises NotImplementedError
    # on the SQLite dialect.
    if is_pg:
        op.create_check_constraint(
            "ck_cookbooks_feedback_mode",
            "cookbooks",
            "feedback_mode IS NULL OR feedback_mode IN ('pat', 'github_app')",
        )
        op.create_check_constraint(
            "ck_cookbooks_feedback_repo_mode",
            "cookbooks",
            "feedback_repo IS NULL OR feedback_mode IS NOT NULL",
        )

    op.add_column(
        "cookbooks",
        sa.Column("feedback_pat_enc", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Remove feedback routing columns from cookbooks."""
    is_pg = op.get_bind().dialect.name == "postgresql"

    if is_pg:
        op.drop_constraint("ck_cookbooks_feedback_repo_mode", "cookbooks", type_="check")
        op.drop_constraint("ck_cookbooks_feedback_mode", "cookbooks", type_="check")

    with op.batch_alter_table("cookbooks") as batch_op:
        batch_op.drop_column("feedback_pat_enc")
        batch_op.drop_column("feedback_mode")
        batch_op.drop_column("feedback_repo")

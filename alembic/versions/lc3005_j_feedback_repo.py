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

Postgres-only SQL — no SQLite dialect needed.
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
    op.add_column(
        "cookbooks",
        sa.Column("feedback_repo", sa.Text(), nullable=True),
    )
    op.add_column(
        "cookbooks",
        sa.Column("feedback_mode", sa.Text(), nullable=True),
    )
    # Enforce: feedback_mode IN ('pat','github_app') when set
    op.create_check_constraint(
        "ck_cookbooks_feedback_mode",
        "cookbooks",
        "feedback_mode IS NULL OR feedback_mode IN ('pat', 'github_app')",
    )
    # Enforce: if repo is set, mode must be set too
    op.create_check_constraint(
        "ck_cookbooks_feedback_repo_mode",
        "cookbooks",
        "feedback_repo IS NULL OR feedback_mode IS NOT NULL",
    )
    # Store encrypted PAT per cookbook (Fernet-encrypted, never plaintext in DB)
    op.add_column(
        "cookbooks",
        sa.Column("feedback_pat_enc", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Remove feedback routing columns from cookbooks."""
    op.drop_constraint("ck_cookbooks_feedback_repo_mode", "cookbooks", type_="check")
    op.drop_constraint("ck_cookbooks_feedback_mode", "cookbooks", type_="check")
    op.drop_column("cookbooks", "feedback_pat_enc")
    op.drop_column("cookbooks", "feedback_mode")
    op.drop_column("cookbooks", "feedback_repo")

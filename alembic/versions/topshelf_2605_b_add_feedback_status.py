"""topshelf_2605/B — add feedback_status column to feedback tables

Revision ID: topshelf_2605_b_add_feedback_status
Revises: rh2605h1_cbt_install_public
Create Date: 2026-05-28 00:00:00.000000

Adds ``feedback_status`` (TEXT NOT NULL DEFAULT 'pending') to both
``feedback_submissions`` and ``recipify_requests`` tables.

State machine values:
  pending  → dispatch was sent; waiting for GitHub workflow to PATCH back
  filed    → workflow ran; issue_url has been populated
  failed   → dispatch failed or workflow never patched back
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "topshelf_2605_b_add_feedback_status"
down_revision = "rh2605h1_cbt_install_public"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add feedback_status column to feedback_submissions and recipify_requests."""
    op.add_column(
        "feedback_submissions",
        sa.Column(
            "feedback_status",
            sa.Text(),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "recipify_requests",
        sa.Column(
            "feedback_status",
            sa.Text(),
            nullable=False,
            server_default="pending",
        ),
    )


def downgrade() -> None:
    """Drop feedback_status from both tables."""
    op.drop_column("recipify_requests", "feedback_status")
    op.drop_column("feedback_submissions", "feedback_status")

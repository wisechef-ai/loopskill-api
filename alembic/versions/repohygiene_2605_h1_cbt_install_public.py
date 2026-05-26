"""repohygiene_2605/H.1 — cbt_token can install public-catalog skills (Issue #290)

Revision ID: rh2605h1_cbt_install_public
Revises: d8c8a3f721ec
Create Date: 2026-05-26 00:00:00.000000

Option C implementation: add ``allow_public_catalog`` boolean column to
``cookbook_share_tokens``.

  allow_public_catalog = True  → this token may call GET /api/skills/install
                                 for public-catalog skills the cookbook-owner
                                 is entitled to (tier check enforced in the
                                 install route, not here).
  allow_public_catalog = False → original behaviour: token is restricted to
                                 /api/cookbooks/* only.

UPGRADE:
  1. Add column with DEFAULT true (all existing tokens temporarily get True).
  2. Backfill: set allow_public_catalog = FALSE for tokens whose cookbook-owner
     subscription_tier is NOT in ('pro', 'pro_plus', 'operator', 'studio').
     The legacy alias tiers 'operator'/'studio' are treated as pro_plus
     equivalents (see access_routes.TIER_RANK; remove after 2026-06-10).

DOWNGRADE:
  Drop the column. Tokens revert to the original strict path restriction
  (middleware decides — no data loss, only capability loss).

Postgres-only DDL conventions per alembic-postgres-only-sql-discipline skill:
  ALTER TABLE operations done via op.execute for Postgres; the SQLite test path
  uses the ORM-level column addition through op.add_column which SQLite
  supports for simple ADD COLUMN.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "rh2605h1_cbt_install_public"
down_revision = "d8c8a3f721ec"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add allow_public_catalog; backfill False for non-pro/non-pro_plus owners."""
    # Step 1: Add the column. op.add_column works on both Postgres and SQLite.
    op.add_column(
        "cookbook_share_tokens",
        sa.Column(
            "allow_public_catalog",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )

    # Step 2: Backfill — set allow_public_catalog = false for tokens whose
    # cookbook-owner tier is NOT a pro-equivalent.
    #
    # Pro-equivalent tiers: 'pro', 'pro_plus', 'operator', 'studio'
    # (operator/studio are 30-day legacy aliases; include them so we don't
    # regress existing tokens for legacy-tier owners who are effectively pro+).
    #
    # The join path: cookbook_share_tokens → cookbooks → users
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        op.execute(
            """
            UPDATE cookbook_share_tokens AS cst
            SET    allow_public_catalog = false
            FROM   cookbooks            AS cb
            JOIN   users                AS u  ON u.id = cb.cookbook_owner
            WHERE  cb.id  = cst.cookbook_id
            AND    u.subscription_tier NOT IN ('pro', 'pro_plus', 'operator', 'studio')
            """
        )
    else:
        # SQLite: correlated UPDATE — SQLite ≥ 3.15 supports this via subquery.
        op.execute(
            """
            UPDATE cookbook_share_tokens
            SET    allow_public_catalog = 0
            WHERE  cookbook_id IN (
                SELECT cb.id
                FROM   cookbooks cb
                JOIN   users u ON u.id = cb.cookbook_owner
                WHERE  u.subscription_tier NOT IN ('pro', 'pro_plus', 'operator', 'studio')
            )
            """
        )


def downgrade() -> None:
    """Drop allow_public_catalog — tokens revert to strict cookbook-only path."""
    op.drop_column("cookbook_share_tokens", "allow_public_catalog")

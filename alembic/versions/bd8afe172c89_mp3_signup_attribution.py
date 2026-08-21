"""money-path-3: add users.signup_attribution (first-touch UTM/ref)

Revision ID: bd8afe172c89
Revises: bundles0811_p1_slug_backfill
Create Date: 2026-08-12

2026-08-12 money-path audit, Fix #3: first-touch UTM/ref attribution capture
at signup. Adds ``users.signup_attribution`` — a nullable JSON blob written
ONCE, inside the OAuth callback that creates the row (see
app._skill_helpers.resolve_signup_attribution, app/auth_routes.py), holding
{utm_source, utm_medium, utm_campaign, utm_content, ref, captured_at}.

Deliberately a NEW column, not a repurposing of the existing ``utm_ref``
(String(32)) column — that column is written by a DIFFERENT writer (the
Stripe subscription webhook, weeks later at paid-conversion time, sourced
from checkout session metadata) with different validation rules (a narrow
platform-code allowlist). Reusing it would make the two writers' "don't
overwrite if already set" guards fight each other.

Uses the plain ``JSON`` SQLAlchemy type (matches the existing convention in
this file for cross-dialect JSON columns, e.g. Skill.related_skills,
Bundle.signals) — Postgres stores it as JSON, SQLite as TEXT-backed JSON.
Plain ADD COLUMN, nullable, no default, no backfill: purely additive, safe
on the populated prod ``users`` table, and needs none of the Postgres-only
DDL discipline (no PL/pgSQL, no partial index, no CHECK constraint).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bd8afe172c89"
down_revision: Union[str, Sequence[str], None] = "agentreg0819_agent_identities"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("signup_attribution", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "signup_attribution")

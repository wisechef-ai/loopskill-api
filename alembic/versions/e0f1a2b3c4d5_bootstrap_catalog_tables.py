"""bootstrap baseline catalog tables

Revision ID: e0f1a2b3c4d5
Revises: 4ba0bf05cd47
Create Date: 2026-06-23 00:00:00.000000

Background
----------
The baseline revision (``4ba0bf05cd47``) is a deliberate no-op stamp — it
documents the production schema state at Sprint 4 but creates nothing. On
PRODUCTION this was fine: ``skills``, ``telemetry_events``,
``carousel_entries``, and ``install_events`` already existed (created
out-of-band by ``Base.metadata.create_all()`` before alembic was wired in).

On a FRESH database — exactly what every self-hoster runs on first boot —
the next migration (``a7f7db696591``) immediately tries to ALTER TABLE
on tables that don't exist yet, producing:

    OperationalError: no such table: telemetry_events

This revision closes that gap for all four tables.  Every ``op.create_table``
call is guarded by an existence check so the migration is a verifiable no-op
on any database that already has the tables (production, pre-existing dev DBs).

Column sets
-----------
Each table is created with ONLY the columns present at the baseline revision —
that is, the columns that existed BEFORE ``a7f7db696591`` (which adds
skill_id, goal_class, duration_seconds, retry_count, user_intervention,
agent_class_hash, install_event_id to telemetry_events; slot, role, verdict,
score to carousel_entries; vertical, rating_avg, install_count, is_free to
skills).  Including those columns here would produce duplicate-column errors
when ``a7f7db696591`` runs.

FK handling
-----------
``skills.creator_id`` and ``skills.org_id`` are left as plain nullable UUID
columns without FK constraints: the ``creators`` and ``orgs`` tables are not
present in the migration chain at this point (they are bootstrapped later by
``a8b9c0d1e2f3``).  SQLite does not enforce FK constraints by default anyway;
on Postgres a FK that references a non-existent table would cause CREATE TABLE
to fail.

``carousel_entries.skill_id`` and ``install_events.skill_id`` do carry FK
constraints because ``skills`` is created earlier in the same upgrade() call.

``install_events.api_key_id`` is intentionally left without a FK constraint
(``api_keys`` is also bootstrapped by ``a8b9c0d1e2f3``, which comes later).

Production-safety
-----------------
All CREATE TABLE calls are guarded by ``inspector.get_table_names()``.  A
production database where these tables already exist will skip every CREATE
and every index creation — this migration is provably a no-op there.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e0f1a2b3c4d5"
down_revision: Union[str, Sequence[str], None] = "4ba0bf05cd47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ── dialect helpers ────────────────────────────────────────────────────────────


def _is_pg() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _uuid_pk() -> sa.Column:
    """UUID primary-key column, dialect-aware."""
    if _is_pg():
        return sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        )
    return sa.Column("id", sa.String(36), primary_key=True, nullable=False)


def _uuid_col(name: str, *, nullable: bool = True, fk: str | None = None) -> sa.Column:
    """UUID column (with optional FK), dialect-aware."""
    col_type = postgresql.UUID(as_uuid=True) if _is_pg() else sa.String(36)
    if fk:
        return sa.Column(name, col_type, sa.ForeignKey(fk), nullable=nullable)
    return sa.Column(name, col_type, nullable=nullable)


# ── upgrade ───────────────────────────────────────────────────────────────────


def upgrade() -> None:
    """Create the four baseline catalog tables if they don't already exist."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    # ── skills ─────────────────────────────────────────────────────────────
    # Baseline columns only. creator_id and org_id are plain nullable UUID
    # columns without FK constraints — creators/orgs are bootstrapped later
    # (a8b9c0d1e2f3) so we cannot reference them here without Postgres failing.
    if "skills" not in existing:
        op.create_table(
            "skills",
            _uuid_pk(),
            sa.Column("slug", sa.String(255), unique=True, nullable=False),
            sa.Column("title", sa.String(512), nullable=False),
            sa.Column("description", sa.Text, nullable=True),
            sa.Column("category", sa.String(128), nullable=True),
            sa.Column("readme", sa.Text, nullable=True),
            sa.Column("license", sa.String(64), nullable=True),
            sa.Column("tier", sa.String(32), nullable=True),
            sa.Column(
                "is_public",
                sa.Boolean,
                server_default=sa.text("true"),
                nullable=False,
            ),
            # FK to creators.id / orgs.id omitted intentionally — those tables
            # don't exist yet; see module docstring for the full rationale.
            _uuid_col("creator_id", nullable=True),
            _uuid_col("org_id", nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=True,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=True,
            ),
        )
        op.create_index("ix_skills_slug", "skills", ["slug"], unique=True)
        op.create_index("ix_skills_category", "skills", ["category"])

    # ── telemetry_events ───────────────────────────────────────────────────
    # Baseline columns only. The typed columns (skill_id, goal_class, …) are
    # added by the next migration (a7f7db696591) — do NOT include them here.
    if "telemetry_events" not in existing:
        op.create_table(
            "telemetry_events",
            _uuid_pk(),
            sa.Column("event_type", sa.String(128), nullable=False),
            sa.Column("skill_slug", sa.String(255), nullable=True),
            sa.Column("payload", sa.Text, nullable=True),
            sa.Column("client_ip", sa.String(64), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=True,
            ),
        )
        op.create_index("ix_telemetry_events_event_type", "telemetry_events", ["event_type"])
        op.create_index("ix_telemetry_events_skill_slug", "telemetry_events", ["skill_slug"])

    # ── carousel_entries ───────────────────────────────────────────────────
    # Baseline columns only. scoring columns (slot, role, verdict, score) are
    # added by a7f7db696591 — do NOT include them here.
    # skills is created above, so the FK is valid on Postgres.
    if "carousel_entries" not in existing:
        op.create_table(
            "carousel_entries",
            _uuid_pk(),
            _uuid_col("skill_id", nullable=False, fk="skills.id"),
            sa.Column("featured_date", sa.DateTime, nullable=False),
            sa.Column("tagline", sa.String(512), nullable=True),
            sa.Column("position", sa.Integer, server_default="0", nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=True,
            ),
        )
        op.create_index("ix_carousel_entries_featured_date", "carousel_entries", ["featured_date"])

    # ── install_events ─────────────────────────────────────────────────────
    # Baseline columns only.
    # - skill_id FK to skills.id: valid (skills created above).
    # - api_key_id: plain nullable UUID — api_keys is bootstrapped by a8b9c0d1e2f3
    #   which comes later; cannot FK-reference it here.
    # - status (added by f1a9c0d3e711), cookbook_id/attribution (spotify_0608_e)
    #   are NOT included here — each adds the column in its own migration.
    if "install_events" not in existing:
        op.create_table(
            "install_events",
            _uuid_pk(),
            _uuid_col("skill_id", nullable=False, fk="skills.id"),
            sa.Column("skill_slug", sa.String(255), nullable=True),
            _uuid_col("api_key_id", nullable=True),  # no FK — api_keys not yet in chain
            sa.Column("version_semver", sa.String(32), nullable=True),
            sa.Column("client_ip", sa.String(64), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=True,
            ),
        )
        op.create_index("ix_install_events_skill_id", "install_events", ["skill_id"])
        op.create_index("ix_install_events_skill_slug", "install_events", ["skill_slug"])

    # ── orgs ───────────────────────────────────────────────────────────────
    # create_all-only orphan (never created by a migration, never ALTERed later).
    # Safe to create with its full current column set.
    if "orgs" not in existing:
        op.create_table(
            "orgs",
            _uuid_pk(),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("slug", sa.String(255), unique=True, nullable=False),
            sa.Column("api_key_hash", sa.String(255), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=True,
            ),
        )
        op.create_index("ix_orgs_slug", "orgs", ["slug"], unique=True)

    # ── api_library ────────────────────────────────────────────────────────
    # create_all-only orphan. Full column set; no FKs.
    if "api_library" not in existing:
        op.create_table(
            "api_library",
            _uuid_pk(),
            sa.Column("slug", sa.String(255), unique=True, nullable=False),
            sa.Column("title", sa.String(512), nullable=False),
            sa.Column("description", sa.Text, nullable=True),
            sa.Column("content", sa.Text, nullable=True),
            sa.Column("category", sa.String(128), nullable=True),
            sa.Column("base_url", sa.Text, nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=True,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=True,
            ),
        )
        op.create_index("ix_api_library_slug", "api_library", ["slug"], unique=True)

    # ── recipes ────────────────────────────────────────────────────────────
    # Dead legacy table (Phase 3 drops it), but the ORM still maps it, so a
    # create_all-vs-alembic schema diff flags it as missing. Create it for
    # replay parity. creator_id FK to creators.id is omitted (creators is
    # bootstrapped later by a8b9c0d1e2f3) — plain nullable UUID, same rationale
    # as skills.creator_id above.
    if "recipes" not in existing:
        op.create_table(
            "recipes",
            _uuid_pk(),
            sa.Column("slug", sa.String(255), unique=True, nullable=False),
            sa.Column("title", sa.String(512), nullable=False),
            sa.Column("description", sa.Text, nullable=True),
            sa.Column("content", sa.Text, nullable=True),
            sa.Column("category", sa.String(128), nullable=True),
            sa.Column(
                "is_public",
                sa.Boolean,
                server_default=sa.text("true"),
                nullable=True,
            ),
            _uuid_col("creator_id", nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=True,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=True,
            ),
        )
        op.create_index("ix_recipes_slug", "recipes", ["slug"], unique=True)

    # ── wisechef_demo_requests ─────────────────────────────────────────────
    # create_all-only orphan. Full column set; no FKs.
    if "wisechef_demo_requests" not in existing:
        op.create_table(
            "wisechef_demo_requests",
            _uuid_pk(),
            sa.Column("email", sa.String(512), nullable=False),
            sa.Column("company_name", sa.String(255), nullable=True),
            sa.Column("company_size", sa.String(32), nullable=True),
            sa.Column("source", sa.String(128), nullable=True),
            sa.Column("message", sa.Text, nullable=True),
            sa.Column("status", sa.String(32), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=True,
            ),
            sa.Column("contacted_at", sa.DateTime(timezone=True), nullable=True),
        )


# ── downgrade ─────────────────────────────────────────────────────────────────


def downgrade() -> None:
    """Drop the four baseline catalog tables.

    Tables are dropped in reverse FK dependency order so Postgres FK
    constraints are satisfied:
      install_events → skills  (drop install_events first)
      carousel_entries → skills  (drop carousel_entries next)
      telemetry_events  (no FK to skills at baseline — that's added by a7f7db696591
                         and removed again by a7f7db696591's downgrade before we reach here)
      skills  (last, after all referencing tables are gone)

    In practice this downgrade only runs in local dev / CI rollback.
    Production never downgrades through this point (all prod DBs had these
    tables created out-of-band before alembic was introduced).
    """
    # Use DROP TABLE IF EXISTS so the downgrade is idempotent even if the
    # upgrade was partially applied or these tables were created out-of-band.
    for table in (
        "wisechef_demo_requests",
        "recipes",
        "api_library",
        "orgs",
        "install_events",
        "carousel_entries",
        "telemetry_events",
        "skills",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table}")

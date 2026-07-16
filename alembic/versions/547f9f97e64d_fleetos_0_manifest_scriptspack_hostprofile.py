"""fleetos_1607 Phase 0 — loop_manifests + scripts_packs + host_profiles

Revision ID: 547f9f97e64d
Revises: e13d73700587
Create Date: 2026-07-16

Phase 0 of fleetos_1607: the declarative fleet-artifact primitives that turn
LoopSkill from a marketplace into the control plane for AI agent fleets.

Ships three additive tables — no ALTER of existing schema, no data migration:
  * loop_manifests  — slim v1 desired-state of ONE loop (schedule, prompt,
                      skill locks, typed requires{}, secret_refs[], state_class,
                      safety_class + a `reserved` JSON blob for the ~25
                      documented-not-implemented fields per §0 #16d).
  * scripts_packs   — signed content-addressed tarball of a scripts dir
                      (sha256 identity, per-entry modes, symlink policy,
                      secret_scan_clean gate).
  * host_profiles   — LITE substrate contract (os / runtimes / packages).

The `soul` artifact type was DELETED as a new table by the 5-step deletion pass
(§0 #7/#16): the existing `personalities` table already IS the deployable-SOUL
primitive; a golden bundle references a Personality row.

Portable SQL only (plain CREATE TABLE / CHECK / UNIQUE / INDEX) — applies
identically on Postgres (prod) and SQLite (test fixture). No PL/pgSQL, no
Postgres-only defaults; timestamps use func.now() to match the repo's existing
convention (FleetMember, Bundle, …), which SQLite honours at INSERT.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "547f9f97e64d"
down_revision: Union[str, Sequence[str], None] = "e13d73700587"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _uuid() -> sa.types.TypeEngine:
    """UUID on Postgres, CHAR(32) on SQLite — mirrors the repo's UUID(as_uuid=True) usage."""
    return postgresql.UUID(as_uuid=True).with_variant(sa.CHAR(32), "sqlite")


def upgrade() -> None:
    """Upgrade schema — three additive tables."""
    op.create_table(
        "loop_manifests",
        sa.Column("id", _uuid(), primary_key=True, nullable=False),
        sa.Column("loop_id", sa.String(length=128), nullable=False),
        sa.Column("manifest_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("owner_user_id", _uuid(), nullable=True),
        sa.Column("org_id", _uuid(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("schedule", sa.String(length=128), nullable=False),
        sa.Column("tz", sa.String(length=64), nullable=False, server_default="UTC"),
        sa.Column("concurrency_policy", sa.String(length=16), nullable=False, server_default="forbid"),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("skills", sa.JSON(), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("deliver", sa.String(length=255), nullable=True),
        sa.Column("requires", sa.JSON(), nullable=False),
        sa.Column("secret_refs", sa.JSON(), nullable=False),
        sa.Column("state_class", sa.String(length=24), nullable=False, server_default="stateless"),
        sa.Column("state_locator", sa.Text(), nullable=True),
        sa.Column("timeout_seconds", sa.Integer(), nullable=True),
        sa.Column("safety_class", sa.String(length=16), nullable=False, server_default="best-effort"),
        sa.Column("reserved", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("loop_id", "owner_user_id", "org_id", name="uq_loop_manifest_scope"),
        sa.CheckConstraint(
            "concurrency_policy IN ('forbid','allow','replace')",
            name="ck_loop_manifest_concurrency_policy",
        ),
        sa.CheckConstraint(
            "state_class IN ('stateless','external','local-resettable','local-required')",
            name="ck_loop_manifest_state_class",
        ),
        sa.CheckConstraint(
            "safety_class IN ('idempotent','best-effort','manual-only','fenced')",
            name="ck_loop_manifest_safety_class",
        ),
    )
    op.create_index("idx_loop_manifest_owner", "loop_manifests", ["owner_user_id", "loop_id"])
    op.create_index(op.f("ix_loop_manifests_loop_id"), "loop_manifests", ["loop_id"])
    op.create_index(op.f("ix_loop_manifests_owner_user_id"), "loop_manifests", ["owner_user_id"])
    op.create_index(op.f("ix_loop_manifests_org_id"), "loop_manifests", ["org_id"])

    op.create_table(
        "scripts_packs",
        sa.Column("id", _uuid(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=True),
        sa.Column("org_id", _uuid(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("tarball_path", sa.Text(), nullable=True),
        sa.Column("tarball_size_bytes", sa.Integer(), nullable=True),
        sa.Column("entries", sa.JSON(), nullable=False),
        sa.Column("symlink_policy", sa.String(length=24), nullable=False, server_default="reject"),
        sa.Column("secret_scan_clean", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("owner_user_id", "org_id", "sha256", name="uq_scripts_pack_scope_sha"),
        sa.CheckConstraint(
            "symlink_policy IN ('reject','preserve-internal','follow')",
            name="ck_scripts_pack_symlink_policy",
        ),
    )
    op.create_index(op.f("ix_scripts_packs_sha256"), "scripts_packs", ["sha256"])
    op.create_index(op.f("ix_scripts_packs_owner_user_id"), "scripts_packs", ["owner_user_id"])
    op.create_index(op.f("ix_scripts_packs_org_id"), "scripts_packs", ["org_id"])

    op.create_table(
        "host_profiles",
        sa.Column("id", _uuid(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=True),
        sa.Column("org_id", _uuid(), nullable=True),
        sa.Column("os", sa.JSON(), nullable=False),
        sa.Column("runtimes", sa.JSON(), nullable=False),
        sa.Column("packages", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("name", "owner_user_id", "org_id", name="uq_host_profile_scope_name"),
    )
    op.create_index(op.f("ix_host_profiles_owner_user_id"), "host_profiles", ["owner_user_id"])
    op.create_index(op.f("ix_host_profiles_org_id"), "host_profiles", ["org_id"])


def downgrade() -> None:
    """Downgrade schema — drop the three tables."""
    op.drop_index(op.f("ix_host_profiles_org_id"), table_name="host_profiles")
    op.drop_index(op.f("ix_host_profiles_owner_user_id"), table_name="host_profiles")
    op.drop_table("host_profiles")

    op.drop_index(op.f("ix_scripts_packs_org_id"), table_name="scripts_packs")
    op.drop_index(op.f("ix_scripts_packs_owner_user_id"), table_name="scripts_packs")
    op.drop_index(op.f("ix_scripts_packs_sha256"), table_name="scripts_packs")
    op.drop_table("scripts_packs")

    op.drop_index(op.f("ix_loop_manifests_org_id"), table_name="loop_manifests")
    op.drop_index(op.f("ix_loop_manifests_owner_user_id"), table_name="loop_manifests")
    op.drop_index(op.f("ix_loop_manifests_loop_id"), table_name="loop_manifests")
    op.drop_index("idx_loop_manifest_owner", table_name="loop_manifests")
    op.drop_table("loop_manifests")

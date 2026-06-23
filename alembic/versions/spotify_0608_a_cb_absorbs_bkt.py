"""spotify_0608/A — Cookbook absorbs Bucket (D1)

Revision ID: spotify_0608_a_cookbook_absorbs_bucket
Revises: superset_0606_b_fed_cache
Create Date: 2026-06-09 00:00:00.000000

spotify_0608 Phase A. Cookbook becomes the survivor primitive (D1). This
migration:

  1. Adds the Bucket-absorbed public/white-label columns to ``cookbooks``:
     slug, visibility, is_white_label, custom_domain, pin_mode, theme_json.
  2. Creates ``cookbook_deployments`` — the lossless replacement for
     ``bucket_skills`` (R3 data-model contract). Same shape: id PK, cookbook_id
     FK, skill_id NULLABLE, fork_id NULLABLE, version_pin, install_order, with
     the SAME ``skill_id XOR fork_id`` CHECK constraint BucketSkill carried.
  3. Migrates ``buckets`` → ``cookbooks`` 1:1, REUSING the bucket UUID as the
     cookbook id so the deployment FK remap is a straight copy. Migrated rows
     get is_base=false, parent_cookbook_id=NULL, cookbook_owner=bucket.owner_id.
  4. Migrates ``bucket_skills`` → ``cookbook_deployments`` 1:1 (id + all columns
     copied; bucket_id → cookbook_id is the same UUID from step 3).
  5. Drops ``bucket_skills`` then ``buckets``.

The data-copy steps run only on Postgres (prod). On a fresh CI/testcontainer
run the buckets tables exist but are empty, so the copies are no-ops.

DOWNGRADE: recreate buckets + bucket_skills, copy rows back (cookbooks that
carry a slug AND have a matching legacy shape), drop cookbook_deployments and
the six added columns. Best-effort — the round-trip is lossless for rows that
originated as buckets; net-new public cookbooks created after this migration
have no bucket to map back to and are left in place (their slug/visibility
columns simply disappear with the column drop).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "spotify_0608_a_cb_absorbs_bkt"
down_revision = "superset_0606_b_fed_cache"
branch_labels = None
depends_on = None


def _uuid_type(is_pg: bool):
    """UUID column type for Postgres, String(36) elsewhere (SQLite)."""
    return postgresql.UUID(as_uuid=True) if is_pg else sa.String(length=36)


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # ── 1. Add Bucket-absorbed columns to cookbooks ──────────────────────
    op.add_column("cookbooks", sa.Column("slug", sa.String(length=255), nullable=True))
    op.add_column(
        "cookbooks",
        sa.Column("visibility", sa.String(length=32), nullable=False, server_default="private"),
    )
    op.add_column(
        "cookbooks",
        sa.Column("is_white_label", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("cookbooks", sa.Column("custom_domain", sa.Text(), nullable=True))
    op.add_column(
        "cookbooks",
        sa.Column("pin_mode", sa.String(length=32), nullable=False, server_default="latest-stable"),
    )
    op.add_column("cookbooks", sa.Column("theme_json", sa.JSON(), nullable=True))
    op.create_index("ix_cookbooks_slug", "cookbooks", ["slug"], unique=True)
    op.create_index("ix_cookbooks_custom_domain", "cookbooks", ["custom_domain"], unique=False)

    # ── 2. Create cookbook_deployments ───────────────────────────────────
    op.create_table(
        "cookbook_deployments",
        sa.Column("id", _uuid_type(is_pg), primary_key=True),
        sa.Column(
            "cookbook_id",
            _uuid_type(is_pg),
            sa.ForeignKey("cookbooks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "skill_id",
            _uuid_type(is_pg),
            sa.ForeignKey("skills.id"),
            nullable=True,
        ),
        sa.Column(
            "fork_id",
            _uuid_type(is_pg),
            nullable=True,
        ),
        sa.Column("version_pin", sa.String(length=64), nullable=True),
        sa.Column("install_order", sa.Integer(), nullable=False, server_default="100"),
        sa.CheckConstraint(
            "(skill_id IS NOT NULL) <> (fork_id IS NOT NULL)",
            name="ck_cookbook_deployments_skill_xor_fork",
        ),
    )
    op.create_index("ix_cookbook_deployments_cookbook_id", "cookbook_deployments", ["cookbook_id"])
    op.create_index(
        "ix_cookbook_deployments_order", "cookbook_deployments", ["cookbook_id", "install_order"]
    )

    # ── 3 + 4. Data migration (Postgres only; no-op on empty CI tables) ──
    if is_pg:
        # buckets → cookbooks (reuse bucket.id as cookbook.id)
        op.execute(
            sa.text(
                """
                INSERT INTO cookbooks
                    (id, name, description, is_base, cookbook_owner,
                     slug, visibility, is_white_label, custom_domain, pin_mode,
                     theme_json, created_at, updated_at)
                SELECT
                    b.id, b.name, b.description, false, b.owner_id,
                    b.slug, b.visibility, b.is_white_label, b.custom_domain, b.pin_mode,
                    b.theme_json, b.created_at, b.updated_at
                FROM buckets b
                ON CONFLICT (id) DO NOTHING
                """
            )
        )
        # bucket_skills → cookbook_deployments (bucket_id is now a cookbook id)
        op.execute(
            sa.text(
                """
                INSERT INTO cookbook_deployments
                    (id, cookbook_id, skill_id, fork_id, version_pin, install_order)
                SELECT
                    bs.id, bs.bucket_id, bs.skill_id, bs.fork_id, bs.version_pin, bs.install_order
                FROM bucket_skills bs
                ON CONFLICT (id) DO NOTHING
                """
            )
        )

    # ── 5. Drop legacy tables ────────────────────────────────────────────
    op.drop_table("bucket_skills")
    op.drop_table("buckets")


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # Recreate buckets
    op.create_table(
        "buckets",
        sa.Column("id", _uuid_type(is_pg), primary_key=True),
        sa.Column(
            "owner_id",
            _uuid_type(is_pg),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("visibility", sa.String(length=32), nullable=False, server_default="private"),
        sa.Column("is_white_label", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("custom_domain", sa.Text(), nullable=True),
        sa.Column("pin_mode", sa.String(length=32), nullable=False, server_default="latest-stable"),
        sa.Column("theme_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.UniqueConstraint("slug", name="uq_buckets_slug"),
    )
    op.create_index("ix_buckets_owner_id", "buckets", ["owner_id"])
    op.create_index("ix_buckets_custom_domain", "buckets", ["custom_domain"])
    op.create_table(
        "bucket_skills",
        sa.Column("id", _uuid_type(is_pg), primary_key=True),
        sa.Column(
            "bucket_id",
            _uuid_type(is_pg),
            sa.ForeignKey("buckets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "skill_id",
            _uuid_type(is_pg),
            sa.ForeignKey("skills.id"),
            nullable=True,
        ),
        sa.Column(
            "fork_id",
            _uuid_type(is_pg),
            nullable=True,
        ),
        sa.Column("version_pin", sa.String(length=64), nullable=True),
        sa.Column("install_order", sa.Integer(), nullable=False, server_default="100"),
    )
    op.create_index("ix_bucket_skills_bucket_id", "bucket_skills", ["bucket_id"])
    op.create_index(
        "ix_bucket_skills_bucket_install_order", "bucket_skills", ["bucket_id", "install_order"]
    )

    if is_pg:
        # cookbooks with a slug that originated as a bucket → copy back.
        op.execute(
            sa.text(
                """
                INSERT INTO buckets
                    (id, owner_id, name, slug, description, visibility,
                     is_white_label, custom_domain, pin_mode, theme_json,
                     created_at, updated_at)
                SELECT
                    c.id, c.cookbook_owner, c.name, c.slug, c.description, c.visibility,
                    c.is_white_label, c.custom_domain, c.pin_mode, c.theme_json,
                    c.created_at, c.updated_at
                FROM cookbooks c
                WHERE c.slug IS NOT NULL AND c.cookbook_owner IS NOT NULL
                ON CONFLICT (id) DO NOTHING
                """
            )
        )
        op.execute(
            sa.text(
                """
                INSERT INTO bucket_skills
                    (id, bucket_id, skill_id, fork_id, version_pin, install_order)
                SELECT
                    d.id, d.cookbook_id, d.skill_id, d.fork_id, d.version_pin, d.install_order
                FROM cookbook_deployments d
                JOIN buckets b ON b.id = d.cookbook_id
                ON CONFLICT (id) DO NOTHING
                """
            )
        )

    op.drop_index("ix_cookbook_deployments_order", table_name="cookbook_deployments")
    op.drop_index("ix_cookbook_deployments_cookbook_id", table_name="cookbook_deployments")
    op.drop_table("cookbook_deployments")
    op.drop_index("ix_cookbooks_custom_domain", table_name="cookbooks")
    op.drop_index("ix_cookbooks_slug", table_name="cookbooks")
    for col in ("theme_json", "pin_mode", "custom_domain", "is_white_label", "visibility", "slug"):
        op.drop_column("cookbooks", col)

"""spotify_2607 Phase A — let a federated like land in BundleSkill.

Revision ID: spotify2607_a_liked_federated
Revises: ah0723_composite_loop_tags
Create Date: 2026-07-26

spotify_2607 Phase A (plan §0 decision #3 / §0b L6 supersession). Until this
sprint ``BundleSkill.skill_id`` was part of a composite primary key
``(bundle_id, skill_id)``, so it could never be NULL — which made the
deployable Liked bundle silently drop every federated like. 76% of the
catalog is federated, so that made the library useless for the majority case
(Adam 2026-07-26). This migration:

  1. adds a surrogate ``id`` UUID primary key to ``bundle_skills`` and drops
     the old composite PK (``pk_cookbook_skills`` on prod — the table was
     renamed cookbook_skills → bundle_skills by f1b2c3d4e5a6, which did NOT
     rename the PK constraint);
  2. makes ``skill_id`` nullable;
  3. adds ``federated_source`` / ``federated_slug`` (nullable, indexed) — the
     stable federated identity, same pair shape as ``SkillLike``;
  4. adds two NULL-tolerant UNIQUE constraints (local + federated) so "one row
     per skill per bundle" still holds for both kinds without a
     NULL-in-unique-index false negative;
  5. adds a CHECK constraint enforcing exactly one identity is set (XOR).

IDEMPOTENT: every step is guarded by ``sa.inspect()`` checks because prod has
a history of out-of-band merges and this migration must be a safe no-op on a
DB already at this schema. Postgres-only SQL is used for the partial unique
indexes (NULL semantics) and is guarded by a dialect check so SQLite
self-host / CI still passes.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "spotify2607_a_liked_federated"
down_revision: Union[str, Sequence[str], None] = "ah0723_composite_loop_tags"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    try:
        return any(c["name"] == column for c in insp.get_columns(table))
    # Rationale: a missing table should not crash the migration — treat as
    # "column absent" so the additive step runs (or the table is skipped by the
    # caller). Mirrors the pattern in liked0711_p0 and e9b5c7a3f1d8.
    except Exception:  # noqa: BLE001
        return False


def _has_index(bind, table: str, index: str) -> bool:
    insp = sa.inspect(bind)
    try:
        return any(i["name"] == index for i in insp.get_indexes(table))
    # Rationale: same defensive posture as _has_column.
    except Exception:  # noqa: BLE001
        return False


def _pk_constraint_names(bind, table: str) -> set[str]:
    """Return the set of primary-key constraint names on ``table``.

    Returns an empty set if the table is absent — callers treat that as 'no PK
    to drop', which is correct (a missing table has no constraint to remove).
    """
    insp = sa.inspect(bind)
    try:
        pk = insp.get_pk_constraint(table)
    # Rationale: inspector can raise on absent tables on some dialects.
    except Exception:  # noqa: BLE001
        return set()
    name = pk.get("name") if isinstance(pk, dict) else None
    return {name} if name else set()


def _has_unique_constraint(bind, table: str, name: str) -> bool:
    insp = sa.inspect(bind)
    try:
        return any(c["name"] == name for c in insp.get_unique_constraints(table))
    # Rationale: same defensive posture.
    except Exception:  # noqa: BLE001
        return False


def _has_check_constraint(bind, table: str, name: str) -> bool:
    insp = sa.inspect(bind)
    try:
        return any(c["name"] == name for c in insp.get_check_constraints(table))
    # Rationale: same defensive posture.
    except Exception:  # noqa: BLE001
        return False


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    is_postgres = dialect == "postgresql"
    uuid_type = PG_UUID(as_uuid=True) if is_postgres else sa.String(36)

    existing_pk_names = _pk_constraint_names(bind, "bundle_skills")
    has_id = _has_column(bind, "bundle_skills", "id")
    has_fed_src = _has_column(bind, "bundle_skills", "federated_source")

    # ── POSTGRES PATH — inline DDL, each step independently idempotent ──
    if is_postgres:
        if not has_id:
            op.add_column(
                "bundle_skills",
                sa.Column("id", uuid_type, nullable=True, server_default=sa.text("gen_random_uuid()")),
            )
        bind.execute(sa.text("UPDATE bundle_skills SET id = gen_random_uuid() WHERE id IS NULL"))
        op.alter_column(
            "bundle_skills", "id", existing_type=uuid_type, nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        )
        # Drop the legacy composite PK (named pk_cookbook_skills on prod — the
        # p34 rename moved the table but not its PK constraint name).
        for pk_name in existing_pk_names:
            if pk_name and pk_name != "bundle_skills_pkey":
                op.execute(f'ALTER TABLE bundle_skills DROP CONSTRAINT IF EXISTS "{pk_name}"')
        op.execute("ALTER TABLE bundle_skills DROP CONSTRAINT IF EXISTS bundle_skills_pkey")
        op.execute("ALTER TABLE bundle_skills ADD PRIMARY KEY (id)")

        op.alter_column("bundle_skills", "skill_id", existing_type=uuid_type, nullable=True)
        if not has_fed_src:
            op.add_column("bundle_skills", sa.Column("federated_source", sa.String(64), nullable=True))
            op.add_column("bundle_skills", sa.Column("federated_slug", sa.String(255), nullable=True))
        if not _has_index(bind, "bundle_skills", "ix_bundle_skills_federated"):
            op.create_index(
                "ix_bundle_skills_federated", "bundle_skills", ["federated_source", "federated_slug"]
            )
        # Partial unique indexes — NULLs don't collide, so a federated row
        # (skill_id NULL) doesn't trip the local uniqueness constraint.
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_bundle_skills_bundle_skill "
            "ON bundle_skills (bundle_id, skill_id) WHERE skill_id IS NOT NULL"
        )
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_bundle_skills_bundle_federated "
            "ON bundle_skills (bundle_id, federated_source, federated_slug) "
            "WHERE federated_source IS NOT NULL AND federated_slug IS NOT NULL"
        )
        if not _has_check_constraint(bind, "bundle_skills", "ck_bundle_skills_local_xor_federated"):
            op.create_check_constraint(
                "ck_bundle_skills_local_xor_federated",
                "bundle_skills",
                "(skill_id IS NOT NULL) <> "
                "(federated_source IS NOT NULL AND federated_slug IS NOT NULL)",
            )
        return

    # ── SQLITE PATH — single batch_alter_table rebuild ──────────────────
    # SQLite has no real ALTER for PK / nullability / constraints — batch mode
    # copies the table into a temp, builds the new shape, and swaps. Doing all
    # mutations in ONE batch context avoids multiple full table rebuilds.
    sqlite_uuid_default = sa.text(
        "lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || "
        "'-4' || substr(lower(hex(randomblob(2))), 2) || '-' || "
        "substr('89ab', abs(random()) % 4 + 1, 1) || "
        "substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6)))"
    )

    if not has_id:
        op.add_column("bundle_skills", sa.Column("id", sa.String(36), nullable=True))
        bind.execute(sa.text(f"UPDATE bundle_skills SET id = {sqlite_uuid_default.text}"))
    else:
        bind.execute(sa.text(f"UPDATE bundle_skills SET id = {sqlite_uuid_default.text} WHERE id IS NULL"))

    with op.batch_alter_table("bundle_skills") as batch_op:
        # Promote id to NOT NULL with a server default for future ORM inserts.
        batch_op.alter_column("id", existing_type=sa.String(36), nullable=False, server_default=sqlite_uuid_default)
        # skill_id becomes nullable (federated rows carry NULL here).
        batch_op.alter_column("skill_id", existing_type=sa.String(36), nullable=True)
        if not has_fed_src:
            batch_op.add_column(sa.Column("federated_source", sa.String(64), nullable=True))
            batch_op.add_column(sa.Column("federated_slug", sa.String(255), nullable=True))
        # Swap PK: drop old composite, set new surrogate-id PK.
        batch_op.create_primary_key("bundle_skills_pkey", ["id"])
        # NULL-tolerant uniques (SQLite treats NULLs as distinct in uniques, so
        # plain constraints are correct here — no partial-index needed).
        if not _has_unique_constraint(bind, "bundle_skills", "uq_bundle_skills_bundle_skill"):
            batch_op.create_unique_constraint(
                "uq_bundle_skills_bundle_skill", ["bundle_id", "skill_id"]
            )
        if not _has_unique_constraint(bind, "bundle_skills", "uq_bundle_skills_bundle_federated"):
            batch_op.create_unique_constraint(
                "uq_bundle_skills_bundle_federated",
                ["bundle_id", "federated_source", "federated_slug"],
            )
        if not _has_check_constraint(bind, "bundle_skills", "ck_bundle_skills_local_xor_federated"):
            batch_op.create_check_constraint(
                "ck_bundle_skills_local_xor_federated",
                "(skill_id IS NOT NULL) <> "
                "(federated_source IS NOT NULL AND federated_slug IS NOT NULL)",
            )

    if not _has_index(bind, "bundle_skills", "ix_bundle_skills_federated"):
        op.create_index(
            "ix_bundle_skills_federated", "bundle_skills", ["federated_source", "federated_slug"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    if _has_check_constraint(bind, "bundle_skills", "ck_bundle_skills_local_xor_federated"):
        op.drop_constraint(
            "ck_bundle_skills_local_xor_federated", "bundle_skills", type_="check"
        )
    if is_postgres:
        op.execute("DROP INDEX IF EXISTS uq_bundle_skills_bundle_skill")
        op.execute("DROP INDEX IF EXISTS uq_bundle_skills_bundle_federated")
    else:
        if _has_unique_constraint(bind, "bundle_skills", "uq_bundle_skills_bundle_skill"):
            op.drop_constraint("uq_bundle_skills_bundle_skill", "bundle_skills", type_="unique")
        if _has_unique_constraint(
            bind, "bundle_skills", "uq_bundle_skills_bundle_federated"
        ):
            op.drop_constraint(
                "uq_bundle_skills_bundle_federated", "bundle_skills", type_="unique"
            )
    if _has_index(bind, "bundle_skills", "ix_bundle_skills_federated"):
        op.drop_index("ix_bundle_skills_federated", table_name="bundle_skills")
    if _has_column(bind, "bundle_skills", "federated_slug"):
        op.drop_column("bundle_skills", "federated_slug")
    if _has_column(bind, "bundle_skills", "federated_source"):
        op.drop_column("bundle_skills", "federated_source")
    # Restore skill_id NOT NULL (will fail if federated rows exist — that is
    # the honest signal that downgrade cannot proceed without data loss).
    if is_postgres:
        op.alter_column(
            "bundle_skills",
            "skill_id",
            existing_type=PG_UUID(as_uuid=True),
            nullable=False,
        )
    # We do NOT drop the surrogate id / restore the composite PK in downgrade:
    # the composite PK is not recoverable once rows may carry NULL skill_id,
    # and a partial restore would silently lose the federated rows. Keeping id
    # is harmless (it is an additive column); the downgrade document above the
    # function explains this trade-off.

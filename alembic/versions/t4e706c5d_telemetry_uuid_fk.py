"""fix — telemetry_events.skill_id / install_event_id: VARCHAR(36) → UUID + real FKs.

a7f7db696591 (typed telemetry, Sprint 4) created both columns as unconditional
``sa.String(36)`` with the comment "stored as string for SQLite compat", while
the ORM model (``app/models.py``) declares ``UUID(as_uuid=True)`` +
``ForeignKey(...)`` for both. That is NOT this repo's established pattern: 30+
other migrations use the dialect-conditional form
``postgresql.UUID(as_uuid=True) if is_pg else sa.String(36)``, and
``fc0706b_snap_uuid`` already fixed the identical drift class on
``member_lockfile_snapshots`` with the identical symptom:

    ERROR: operator does not exist: uuid = character varying
    LINE 6: left join install_events ie on ie.id = t.install_event_id

Measured on prod (loopskill, wisechef-hq) 2026-11-17 via information_schema:

    telemetry_events.install_event_id  character varying(36), NULL in 5719/5719
    telemetry_events.skill_id          character varying(36), NULL in 5719/5719
    telemetry_events constraints       PRIMARY KEY only — NO FK has ever existed

So both columns are dead text today: nothing writes them (the only typed writer,
``core_routes.post_telemetry``, resolves ``skill_slug`` → a ``uuid.UUID`` that
can never land in a varchar column on the PG dialect) and nothing reads them —
the card's join is the first thing that ever tried. Real install provenance
lives in ``provenance_records.install_event_id`` (native uuid + real FK,
spotify_0608 Ph E), which is unaffected.

Why (a) widen-to-UUID over (b) shrink-the-model: the model is the intended
contract, the repo has direct precedent (fc0706b) for correcting exactly this,
and (b) would leave the advertised relationship permanently unimplementable on
the production dialect. The measured data makes (a) free: 0 non-null values ⇒
``USING col::uuid`` never touches a value and FK validation passes vacuously.

Self-hosted deployments (this is open-core) may hold data this prod does not, so
the migration REFUSES to run — with remediation SQL — when either column holds a
non-UUID string (the cast would hard-fail) or an orphaned UUID (the FK
validation would fail mid-DDL). Refusing beats a cryptic abort mid-deploy.

Revision ID: t4e706c5d_telemetry_uuid_fk
Revises: ser1116c_incident_report_type
Create Date: 2026-11-17
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "t4e706c5d_telemetry_uuid_fk"
down_revision: str | Sequence[str] | None = "ser1116c_incident_report_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# column → parent table it must reference after the widening
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("skill_id", "skills"),
    ("install_event_id", "install_events"),
)

_UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


def _refuse_if_dirty(bind) -> None:
    """Fail fast, with remediation, on data the cast/FK would reject.

    Doctrine: a migration that would error or destroy rows mid-DDL refuses to
    run instead. Both checks run while the columns are still varchar, so the
    orphan comparison is text = text (no operator mismatch).
    """
    for col, parent in _COLUMNS:
        bad = bind.execute(
            sa.text(
                f"SELECT count(*) FROM telemetry_events "  # noqa: S608 - fixed column names
                f"WHERE {col} IS NOT NULL AND {col} !~ :uuid_re"
            ),
            {"uuid_re": _UUID_RE},
        ).scalar_one()
        if bad:
            raise RuntimeError(
                f"telemetry_events.{col} holds {bad} non-NULL value(s) that are not "
                "valid UUID text, so `ALTER COLUMN ... USING {col}::uuid` would fail. "
                "Inspect them, then either fix or NULL them out, e.g.:\n"
                f"  SELECT id, {col} FROM telemetry_events WHERE {col} IS NOT NULL "
                f"AND {col} !~ '{_UUID_RE}';\n"
                f"  UPDATE telemetry_events SET {col} = NULL WHERE {col} !~ "
                f"'{_UUID_RE}';  -- only if the values are garbage"
            )
        orphan = bind.execute(
            sa.text(  # noqa: S608 - fixed column/table names
                f"SELECT count(*) FROM telemetry_events t "
                f"WHERE t.{col} IS NOT NULL AND NOT EXISTS "
                f"(SELECT 1 FROM {parent} p WHERE p.id::text = t.{col})"
            )
        ).scalar_one()
        if orphan:
            raise RuntimeError(
                f"telemetry_events.{col} holds {orphan} value(s) with no matching "
                f"{parent}.id row; the new FK constraint would fail to validate. "
                "Reconcile or NULL them out first, e.g.:\n"
                f"  SELECT t.id, t.{col} FROM telemetry_events t "
                f"WHERE t.{col} IS NOT NULL AND NOT EXISTS "
                f"(SELECT 1 FROM {parent} p WHERE p.id::text = t.{col});"
            )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite (tests) has no native UUID type and the ORM layer already
        # handles typing there — same carve-out as fc0706b_snap_uuid.
        return

    _refuse_if_dirty(bind)

    for col, _parent in _COLUMNS:
        op.alter_column(
            "telemetry_events",
            col,
            type_=postgresql.UUID(as_uuid=True),
            postgresql_using=f"{col}::uuid",
        )
    op.create_foreign_key(
        "fk_telemetry_events_skill_id",
        "telemetry_events",
        "skills",
        ["skill_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_telemetry_events_install_event_id",
        "telemetry_events",
        "install_events",
        ["install_event_id"],
        ["id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_constraint("fk_telemetry_events_install_event_id", "telemetry_events", type_="foreignkey")
    op.drop_constraint("fk_telemetry_events_skill_id", "telemetry_events", type_="foreignkey")
    for col, _parent in _COLUMNS:
        op.alter_column(
            "telemetry_events",
            col,
            type_=sa.String(36),
            postgresql_using=f"{col}::text",
        )

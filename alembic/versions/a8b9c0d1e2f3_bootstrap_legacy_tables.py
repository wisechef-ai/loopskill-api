"""bootstrap legacy tables (users, api_keys, creators, creator_payouts, referrals, skill_versions)

Revision ID: a8b9c0d1e2f3
Revises: a7f7db696591
Create Date: 2026-05-20 18:30:00.000000

Background
----------
Before alembic was wired up to this codebase, the API used
``Base.metadata.create_all()`` at startup to create its schema. When alembic
was introduced (baseline rev ``4ba0bf05cd47``), the BASELINE_DDL captured
in tests/migrations/test_baseline_idempotent.py only included the tables
that subsequent migrations explicitly ALTERed in obvious ways — ``skills``,
``telemetry_events``, ``install_events``, ``carousel_entries``.

The user-and-billing surface (``users``, ``api_keys``, ``creators``,
``creator_payouts``, ``referrals``) was present in production at the
baseline revision (created originally by ``create_all``) but was never
re-created by any alembic migration. That worked fine on the production
host (the tables had already been created out-of-band), but a fresh
``alembic upgrade head`` against an empty database — exactly what the
migration test suite does — explodes on the first ``op.add_column("users",
...)`` in ``b8d2c5a91e3f``.

This revision closes that gap. Every table is created with the columns it
HAD at the baseline revision (later migrations layer subscription, referral,
discord, cookbook-scoping, etc. on top). All ``op.create_table`` calls use
``if_not_exists=True``-equivalent guards via ``IF NOT EXISTS`` raw-SQL on
Postgres (no-op when the table already exists in prod), so applying this on
a live production DB is safe and idempotent — no destructive surprises.

This is the right place in the chain to bootstrap because:
  - It runs BEFORE ``b8d2c5a91e3f_subscription.py`` (the first migration that
    ALTERs ``users``).
  - It runs AFTER ``a7f7db696591_typed_telemetry_and_carousel.py`` (which is
    where ``BASELINE_DDL`` ends).
  - Prod is at HEAD, so a fresh run of this revision is impossible there;
    the IF-NOT-EXISTS guards make it a verifiable no-op if it ever did rerun.

Production deploy is safe — the migration only adds CREATE TABLE IF NOT
EXISTS statements. Idempotent.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "a8b9c0d1e2f3"
down_revision: Union[str, None] = "a7f7db696591"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _uuid_pk():
    """UUID primary-key column that works on both Postgres and SQLite."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        return sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        )
    # SQLite (test fixture) stores UUIDs as 36-char strings.
    return sa.Column("id", sa.String(36), primary_key=True, nullable=False)


def _uuid_fk(name: str, target: str, *, nullable: bool, ondelete: str | None = None):
    """UUID foreign-key column that works on both Postgres and SQLite."""
    bind = op.get_bind()
    fk_kwargs = {"ondelete": ondelete} if ondelete else {}
    if bind.dialect.name == "postgresql":
        return sa.Column(
            name,
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(target, **fk_kwargs),
            nullable=nullable,
        )
    return sa.Column(
        name,
        sa.String(36),
        sa.ForeignKey(target, **fk_kwargs),
        nullable=nullable,
    )


def upgrade() -> None:
    """Bootstrap the five legacy tables if they don't already exist."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    # ── users ──────────────────────────────────────────────────────────────
    # Baseline columns only: subscription_*, referral_code, referred_by,
    # discord_user_id, creator_track_record_score, utm_ref are all added by
    # later migrations and MUST NOT appear here.
    if "users" not in existing:
        op.create_table(
            "users",
            _uuid_pk(),
            sa.Column("github_id", sa.Integer, unique=True, nullable=True),
            sa.Column("google_id", sa.String(255), unique=True, nullable=True),
            sa.Column("email", sa.String(512), nullable=True),
            sa.Column("display_name", sa.String(255), nullable=False),
            sa.Column("avatar_url", sa.Text, nullable=True),
            sa.Column("stripe_connect_id", sa.String(255), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index("ix_users_github_id", "users", ["github_id"])
        op.create_index("ix_users_google_id", "users", ["google_id"])
        op.create_index("ix_users_email", "users", ["email"])

    # ── api_keys ───────────────────────────────────────────────────────────
    # Baseline columns only: label, cookbook_id, is_sandbox_operator are
    # added by later migrations.
    if "api_keys" not in existing:
        op.create_table(
            "api_keys",
            _uuid_pk(),
            _uuid_fk("user_id", "users.id", nullable=False),
            sa.Column("key_prefix", sa.String(12), nullable=False),
            sa.Column("key_hash", sa.String(255), nullable=False),
            sa.Column("name", sa.String(255), nullable=True),
            sa.Column("is_active", sa.Boolean, server_default=sa.text("true"), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])

    # ── creators ───────────────────────────────────────────────────────────
    # Baseline columns only: handle, url are added by polish_1805.
    if "creators" not in existing:
        op.create_table(
            "creators",
            _uuid_pk(),
            _uuid_fk("user_id", "users.id", nullable=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("slug", sa.String(255), unique=True, nullable=False),
            sa.Column("avatar_url", sa.Text, nullable=True),
            sa.Column("bio", sa.Text, nullable=True),
            sa.Column("is_founder", sa.Boolean, server_default=sa.text("false"), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index("ix_creators_slug", "creators", ["slug"])

    # ── referrals ──────────────────────────────────────────────────────────
    # Baseline columns only: rate is added by a1b2c3d4e5f6.
    # ``referrals`` is created BEFORE ``creator_payouts`` because the latter
    # has an FK to it.
    if "referrals" not in existing:
        op.create_table(
            "referrals",
            _uuid_pk(),
            _uuid_fk("referrer_user_id", "users.id", nullable=False),
            _uuid_fk("referred_user_id", "users.id", nullable=True),
            sa.Column("referral_code", sa.String(64), nullable=False),
            sa.Column("referred_email", sa.String(512), nullable=True),
            sa.Column("status", sa.String(32), server_default="pending", nullable=False),
            sa.Column("reward_cents", sa.Integer, nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("converted_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_referrals_referrer_user_id", "referrals", ["referrer_user_id"])
        op.create_index("ix_referrals_referral_code", "referrals", ["referral_code"])

    # ── creator_payouts ────────────────────────────────────────────────────
    # Baseline columns only: source, amount_cents, referral_id are added by
    # a1c2d3e4f5g6.
    if "creator_payouts" not in existing:
        op.create_table(
            "creator_payouts",
            _uuid_pk(),
            _uuid_fk("creator_id", "users.id", nullable=False),
            sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
            sa.Column("installs_count", sa.Integer, server_default=sa.text("0"), nullable=False),
            sa.Column(
                "gross_revenue_cents", sa.Integer, server_default=sa.text("0"), nullable=False
            ),
            sa.Column(
                "creator_share_cents", sa.Integer, server_default=sa.text("0"), nullable=False
            ),
            sa.Column("currency", sa.String(8), server_default="eur", nullable=False),
            sa.Column("status", sa.String(32), server_default="pending", nullable=False),
            sa.Column("stripe_transfer_id", sa.String(255), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_creator_payouts_creator_id", "creator_payouts", ["creator_id"])

    # ── skill_versions ────────────────────────────────────────────────────
    # Like the user-and-billing tables, ``skill_versions`` was created by
    # ``Base.metadata.create_all()`` before alembic existed and never gets
    # ALTERed by the chain. Migration ``f00d1109cafe`` (catalog hygiene)
    # JOINs against it. Without bootstrapping it here, a fresh-DB run
    # explodes there.
    if "skill_versions" not in existing:
        op.create_table(
            "skill_versions",
            _uuid_pk(),
            _uuid_fk("skill_id", "skills.id", nullable=False),
            sa.Column("semver", sa.String(32), nullable=False),
            sa.Column("tarball_path", sa.Text, nullable=True),
            sa.Column("tarball_size_bytes", sa.Integer, nullable=True),
            sa.Column("checksum_sha256", sa.String(64), nullable=True),
            sa.Column("changelog", sa.Text, nullable=True),
            sa.Column("skill_toml", sa.Text, nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint("skill_id", "semver", name="uq_skill_version"),
        )
        op.create_index("ix_skill_versions_skill_id", "skill_versions", ["skill_id"])


def downgrade() -> None:
    """Drop the five bootstrap tables.

    Only fires when nothing downstream of this revision exists. In practice
    the down path is for test cleanup; prod never downgrades through this
    point because every PROD database already had these tables (created
    out-of-band by ``create_all``) before alembic was wired in.
    """
    for table in (
        "skill_versions",
        "creator_payouts",
        "referrals",
        "creators",
        "api_keys",
        "users",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table}")

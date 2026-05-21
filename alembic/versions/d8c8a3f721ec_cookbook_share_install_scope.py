"""cookbook_share_2105 — add 'install' to share-token scope vocabulary

Revision ID: d8c8a3f721ec
Revises: h2i3j4k5l6m7
Create Date: 2026-05-21 21:35:00.000000

cookbook_share_2105 Phase E. Adds a third value to the
cookbook_share_tokens.scope enum:

  read    → existing — GET-only access to the cookbook
  edit    → existing — full owner-equivalent access to the cookbook
  install → NEW      — read + bulk install + single-skill install routes;
                       CANNOT add/remove skills, cannot create child tokens

Why a third value (vs reusing 'edit' or relaxing 'read'):
  - 'edit' is owner-equivalent: a recipient with an 'edit' share-token could
    delete the donor's skills or grant further tokens. That is more authority
    than "share my cookbook for installation" needs.
  - 'read' is strictly read-only: it blocks POST /install which is the whole
    point of share-with-another-agent. Relaxing 'read' would lose the only
    audit-officer-grade scope we have.
  - 'install' is the least-privilege scope for the "give my cookbook to
    another agent so they can install all my skills" use case in the
    Recipes offering.

UPGRADE:
  Postgres CHECK constraint relaxed to allow {'read','edit','install'}.
  Default scope server-side flipped to 'install' so new tokens default to
  the user-expectation behaviour ("give them a token, they can install").
  Existing tokens are NOT auto-upgraded — they keep their current scope so
  this migration is a non-breaking widen-the-allowed-set change.

  NB: backfill of existing tokens (read→install when cookbook non-empty) is
  intentionally NOT done here. Rationale: a recipient holding a read-only
  token agreed to read-only; auto-upgrading would silently expand their
  authority. Cookbook owners can rotate any token via the existing share-
  token-rotate flow if they want the new scope.

DOWNGRADE:
  Reset any rows currently on scope='install' back to 'read' (the conservative
  choice — they were created post-migration, and 'edit' would be more
  permissive than they had before, so demoting to 'read' is the safe
  reversal). Then re-tighten the CHECK constraint to {'read','edit'}.
  server_default flipped back to 'edit'.

Postgres-only DDL: ALTER TABLE ... DROP CONSTRAINT + ADD CONSTRAINT inside
op.execute. SQLite ignores CHECK constraints at the ALTER level (it never
enforced them at the row level via DDL — only via the original CREATE
TABLE statement); on SQLite the upgrade just flips the SQLAlchemy-level
default. The cookbook-share regression test runs against pgvector to
verify the Postgres path. See alembic-postgres-only-sql-discipline skill.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "d8c8a3f721ec"
down_revision = "h2i3j4k5l6m7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add 'install' to ck_cookbook_share_tokens_scope; default scope → install."""
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        # Drop the existing tight constraint and replace with the widened one.
        # IF EXISTS guard tolerates fresh databases where the prior bootstrap
        # migration may have already left the constraint with a different name
        # (defensive — recipes-api carries a few schema oddities from the
        # pre-alembic create_all era; see recipes-api-baseline-bootstrap-
        # 2026-05-20.md).
        op.execute(
            "ALTER TABLE cookbook_share_tokens "
            "DROP CONSTRAINT IF EXISTS ck_cookbook_share_tokens_scope"
        )
        op.execute(
            "ALTER TABLE cookbook_share_tokens "
            "ADD CONSTRAINT ck_cookbook_share_tokens_scope "
            "CHECK (scope IN ('read', 'edit', 'install'))"
        )
        # Flip the server_default so future inserts without an explicit scope
        # get 'install'. Application-level default is set in
        # app.share_token_routes._create_share_token_service.
        op.alter_column(
            "cookbook_share_tokens",
            "scope",
            existing_type=sa.String(length=8),
            server_default="install",
            existing_nullable=False,
        )
    else:
        # SQLite path: CHECK constraints are baked into CREATE TABLE; ALTER
        # cannot relax them in-place without a table-rebuild. Tests run
        # against fresh sqlite metadata which reads the updated
        # CheckConstraint from app.models, so this branch is a no-op except
        # for the default flip.
        op.alter_column(
            "cookbook_share_tokens",
            "scope",
            existing_type=sa.String(length=8),
            server_default="install",
            existing_nullable=False,
        )


def downgrade() -> None:
    """Best-effort reversal: demote install→read, re-tighten constraint."""
    bind = op.get_bind()
    dialect = bind.dialect.name

    # Demote any rows currently on 'install' to 'read' before re-tightening
    # the CHECK. Doing this on SQLite is fine; it doesn't enforce the new
    # CHECK but the data move keeps semantics symmetric across backends.
    op.execute("UPDATE cookbook_share_tokens SET scope = 'read' WHERE scope = 'install'")

    if dialect == "postgresql":
        op.execute(
            "ALTER TABLE cookbook_share_tokens "
            "DROP CONSTRAINT IF EXISTS ck_cookbook_share_tokens_scope"
        )
        op.execute(
            "ALTER TABLE cookbook_share_tokens "
            "ADD CONSTRAINT ck_cookbook_share_tokens_scope "
            "CHECK (scope IN ('read', 'edit'))"
        )

    op.alter_column(
        "cookbook_share_tokens",
        "scope",
        existing_type=sa.String(length=8),
        server_default="edit",
        existing_nullable=False,
    )

"""converge_0208 P3 — resolution_status on skill_versions.

Revision ID: c0208p3_skillver_status
Revises: a2ed83d0e3b6
Create Date: 2026-08-03 00:00:00.000000

13 of 76 `skill_versions` rows carry a `tarball_path` pointing at
`/storage/skills/...`, a path that no longer exists on wisechef-hq after a
storage migration. Reconcile resolves PINS straight off this row with no way
to say "this version's bytes are gone" — the row existing was mistaken for
the artifact existing.

`resolution_status` gives that state a name:
  'ok'           (default) — tarball_path is expected to resolve.
  'unresolvable' — repair confirmed no artifact exists for this exact version;
                   nothing was fabricated or repointed at a different version's
                   bytes. Mint/reconcile must refuse this version loudly
                   rather than silently install nothing (or the wrong bytes).

`resolution_note` records why, for humans reading the row later.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c0208p3_skillver_status"
down_revision = "a2ed83d0e3b6"
branch_labels = None
depends_on = None

_CK_NAME = "ck_skill_versions_resolution_status"


def upgrade() -> None:
    op.add_column(
        "skill_versions",
        sa.Column(
            "resolution_status",
            sa.Text(),
            nullable=False,
            server_default="ok",
        ),
    )
    op.add_column(
        "skill_versions",
        sa.Column("resolution_note", sa.Text(), nullable=True),
    )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.create_check_constraint(
            _CK_NAME,
            "skill_versions",
            "resolution_status IN ('ok', 'unresolvable')",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_constraint(_CK_NAME, "skill_versions", type_="check")
    op.drop_column("skill_versions", "resolution_note")
    op.drop_column("skill_versions", "resolution_status")

"""loopskill_0622 p34: rename cookbook->bundle (tables + FK columns)

Revision ID: f1b2c3d4e5a6
Revises: loopskill_0622_p8_runnable_types
Create Date: 2026-06-23

DATA-PRESERVING rename of the cookbook domain to bundle. Uses op.rename_table and
batch_alter_table column renames (portable across PostgreSQL prod and SQLite
self-host / tests) so NO data is lost — this is a live product.

Tables:
  cookbooks            -> bundles
  cookbook_skills      -> bundle_skills
  cookbook_share_tokens-> bundle_share_tokens
  cookbook_deployments -> bundle_deployments

Columns (cookbook_id family -> bundle_id family):
  api_keys.cookbook_id            -> bundle_id
  install_events.cookbook_id      -> bundle_id
  reconcile_events.cookbook_id    -> bundle_id
  fleet_subscriptions.cookbook_id -> bundle_id
  bundles.parent_cookbook_id      -> parent_bundle_id
  bundles.cookbook_owner          -> bundle_owner
  bundles.cookbook_link_token     -> bundle_link_token
  bundles.synced_from_cookbook_id -> synced_from_bundle_id
  bundle_skills.cookbook_id       -> bundle_id
  bundle_share_tokens.cookbook_id -> bundle_id
  bundle_deployments.cookbook_id  -> bundle_id

Idempotency: each rename is guarded by an inspector check so the migration is a
no-op on a DB already at the bundle vocabulary (e.g. a fresh sqlite self-host
where ORM create_all is NOT used — alembic is the single schema path post-1b).
On prod (still at cookbook vocab) every rename applies once.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f1b2c3d4e5a6"
down_revision: Union[str, Sequence[str], None] = "loopskill_0622_p8_runnable_types"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (old_table, new_table)
_TABLE_RENAMES: list[tuple[str, str]] = [
    ("cookbooks", "bundles"),
    ("cookbook_skills", "bundle_skills"),
    ("cookbook_share_tokens", "bundle_share_tokens"),
    ("cookbook_deployments", "bundle_deployments"),
]

# table -> list of (old_col, new_col). Table name is the POST-rename name.
_COLUMN_RENAMES: dict[str, list[tuple[str, str]]] = {
    "api_keys": [("cookbook_id", "bundle_id")],
    "install_events": [("cookbook_id", "bundle_id")],
    "reconcile_events": [("cookbook_id", "bundle_id")],
    "fleet_subscriptions": [("cookbook_id", "bundle_id")],
    "bundles": [
        ("parent_cookbook_id", "parent_bundle_id"),
        ("cookbook_owner", "bundle_owner"),
        ("cookbook_link_token", "bundle_link_token"),
        ("synced_from_cookbook_id", "synced_from_bundle_id"),
    ],
    "bundle_skills": [("cookbook_id", "bundle_id")],
    "bundle_share_tokens": [("cookbook_id", "bundle_id")],
    "bundle_deployments": [("cookbook_id", "bundle_id")],
}


def _existing_tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind, table: str) -> set[str]:
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def _rename_columns(table: str, pairs: list[tuple[str, str]], *, forward: bool) -> None:
    bind = op.get_bind()
    cols = _columns(bind, table)
    if not cols:
        return
    with op.batch_alter_table(table) as batch:
        for old, new in pairs:
            src, dst = (old, new) if forward else (new, old)
            if src in cols and dst not in cols:
                batch.alter_column(src, new_column_name=dst)


def upgrade() -> None:
    bind = op.get_bind()
    existing = _existing_tables(bind)

    # 1. Rename tables first (so column-rename batch ops target the new names).
    for old, new in _TABLE_RENAMES:
        if old in existing and new not in existing:
            op.rename_table(old, new)

    # 2. Rename the cookbook_id-family columns to bundle_id-family.
    for table, pairs in _COLUMN_RENAMES.items():
        _rename_columns(table, pairs, forward=True)


def downgrade() -> None:
    bind = op.get_bind()

    # 1. Reverse the column renames (tables are still at the bundle names here).
    for table, pairs in _COLUMN_RENAMES.items():
        _rename_columns(table, pairs, forward=False)

    # 2. Rename tables back.
    existing = _existing_tables(bind)
    for old, new in _TABLE_RENAMES:
        if new in existing and old not in existing:
            op.rename_table(new, old)

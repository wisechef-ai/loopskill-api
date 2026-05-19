"""secfix_1905_h_indexes_for_hot_paths

Revision ID: c4d5e6f7a8b9
Revises: b1c2d3e4f5a6
Create Date: 2026-05-19 23:30:00.000000

Additive migration: adds indexes on two hot read-paths identified in the
secfix_1905 §2 issue table that were missing indexes despite being looked
up on every authenticated request:

  1. api_keys.key_hash   — looked up by hash on every x-api-key request
                           (was a full table scan on high-traffic workers)

  2. cookbook_share_tokens.token_prefix — looked up by prefix to find
     candidates before hmac.compare_digest (already has a model-level Index
     but the DB migration was absent; this back-fills it).

Both operations use CREATE INDEX IF NOT EXISTS so they are safe to run
against DBs where the index was previously created by another path.
Downgrade drops both indexes cleanly.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "c4d5e6f7a8b9"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # api_keys.key_hash — hot path: every authenticated request does this lookup.
    # CREATE INDEX IF NOT EXISTS so it's safe to run on a DB that already has it.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_api_keys_key_hash "
        "ON api_keys (key_hash)"
    )

    # cookbook_share_tokens.token_prefix — middleware uses prefix to pre-filter
    # CBT candidates before the full hmac.compare_digest scan.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_cookbook_share_tokens_token_prefix "
        "ON cookbook_share_tokens (token_prefix)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_cookbook_share_tokens_token_prefix")
    op.execute("DROP INDEX IF EXISTS ix_api_keys_key_hash")

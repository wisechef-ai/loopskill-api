"""bundle fast-path hint: index install_events.client_ip

Revision ID: bhint0823_client_ip
Revises: 1d889f7ebce4
Create Date: 2026-08-23

t_8ccbdbc5 — the bundle fast-path onboarding hint (app/services/bundle_hint.py)
looks up recent direct installs by ``install_events.client_ip`` inside a 24h
window. The column existed (Issue #22) but had no index; the task spec calls
for an indexed lookup. install_events is append-only and client_ip is
nullable (observability-only column) — a plain btree on the column is safe,
tiny on the current row count, and keeps the per-install hint probe off
seq-scans as the table grows.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "bhint0823_client_ip"
down_revision = "1d889f7ebce4"
branch_labels = None
depends_on = None

_INDEX_NAME = "ix_install_events_client_ip"


def upgrade() -> None:
    op.create_index(_INDEX_NAME, "install_events", ["client_ip"])


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="install_events")

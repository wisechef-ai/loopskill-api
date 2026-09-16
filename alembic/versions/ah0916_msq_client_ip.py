"""ah_0916 — client_ip on missing_skill_queries (attributability of anonymous search misses).

Revision ID: ah0916_msq_client_ip
Revises: coldstart0609_is_probe
Create Date: 2026-09-16 21:00:00.000000

WHY THIS EXISTS (the instrument gap, not a feature):

``missing_skill_queries`` is the VOC table behind the weekly demand digest and
the reach feeder's "search misses" line. Every anonymous row in it is currently
UNATTRIBUTABLE: the table records ``user_id`` (NULL for anonymous) and
``is_probe`` (coldstart_0609/A), but not *where the request came from*. So a
signed-out stranger's search and our own scout's search are the same row shape,
and the feeder must classify the whole anonymous bucket as "not demand until it
does" — as of 2026-09-16, 82 rows.

This is precisely the blindness that let 485 fleet echo signals masquerade as
external demand before the 2026-09-09 echo-removal fix. ``is_probe`` closed the
*known*-fleet half (known api-key owners, fixed loopback IPs). It cannot close
the anonymous half, because with no api_key and no user_id there is nothing
left to correlate on.

``record_missing_skill_query()`` has ACCEPTED ``client_ip`` since coldstart_0609
— it just feeds it to ``is_probe_request()`` and then throws it away. Both live
callers (``app/skill_routes.py``, ``app/metasearch_routes.py``) already supply
it from the request. This migration persists the value that is already in hand.

DESIGN NOTES
* Nullable, no server_default: existing rows are genuinely unknown-origin and
  must stay distinguishable from "known to be anonymous-from-IP-X". A backfilled
  '' or '0.0.0.0' would manufacture provenance we do not have.
* ``String(64)`` matches the existing ``client_ip`` columns on
  ``telemetry_events`` / ``install_events`` (e0f1a2b3c4d5 bootstrap) so all
  three read alike — 64 chars fits IPv6 plus any proxy-chain suffix.
* Postgres-and-SQLite safe: a plain nullable ADD COLUMN needs no batch_alter
  dance on either dialect.
* Downgrade drops the column; nothing reads it as a hard dependency.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "ah0916_msq_client_ip"
down_revision = "coldstart0609_is_probe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "missing_skill_queries",
        sa.Column("client_ip", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("missing_skill_queries", "client_ip")

"""ser1116c — incident_reports.report_type (skill-error vs crash provenance)

The public transparency metric ``skill_error_rate_7d`` counted EVERY row of
``incident_reports``. That table is the persistence model for two different
populations:

  * crash reports from ``POST /api/feedback/incident`` — the auto-improve
    agent's own tooling crashed, and
  * agent skill errors from ``POST /api/v1/skill-error`` and MCP
    ``loopskill_report_skill_error`` — an agent says a marketplace skill it ran
    misbehaved.

Only the second is a skill error. The conflation had never been *visible*
because ``_compute_skill_error_rate_7d`` filtered on a non-existent
``created_at`` and its blanket ``except Exception`` laundered the resulting
AttributeError into 0.0 for the metric's whole existence. Repairing that crash
would have shipped the mixed number live to a public, unauthenticated page, so
classification lands in the same change that makes the metric readable.

The three writers now stamp ``report_type`` themselves (server-side only — a
client that could relabel a crash report as a skill error could move a public
metric). This migration adds the column and backfills the historical tail, which
predates any provenance marker.

BACKFILL — and its honest limit
-------------------------------
Existing rows carry no provenance marker, so they are classified from row shape:

    command IS NULL AND exit_code IS NULL AND env_fingerprint holds nothing  ->  skill_error
    everything else                                                          ->  incident

This is a HEURISTIC, not a proof, and it is wrong in BOTH directions:

  * ``POST /api/v1/skill-error`` accepts ``command``, ``exit_code`` and
    ``env_fingerprint`` from its client, so a genuine skill error that arrived
    with those set is backfilled as a crash report and the metric UNDER-counts.
  * a crash report whose client sent nothing but a signature is backfilled as a
    skill error and the metric OVER-counts.

(The MCP rail is the one unambiguous writer: it hard-codes ``command=None``,
``exit_code=None``, ``env_fingerprint={}``.)

Measured against production on 2026-11-16 rather than assumed: the table held 11
rows, and all 11 landed in the single shape bucket ``command NULL / exit_code
NULL / env_fingerprint '{}'``. Every one is a genuine skill error — the traces
quote installed skill versions ("Installed humanizer 2.1.1 (sha256 9d3eb3…") and
the ``agent_fp_anon`` values are agent identities (``mcp-tool``,
``codex-skill-reporter``, ``hermes-agent-adam``). Zero crash reports have ever
been filed. So on the real historical tail this backfill is exactly correct, and
the residual risk above is about rows that do not exist — but it is stated
because it is a property of the rule, not of today's data.

The backfill runs in Python rather than as one UPDATE so it behaves identically
on Postgres (prod, CI matrix) and SQLite (test harness), where JSON columns are
stored as text and ``'{}'::json`` casts do not exist.

DOWNGRADE: drop the index and the column. The rows themselves are untouched.
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "ser1116c_incident_report_type"
# Single head at authoring (verified with `alembic heads`, 2026-11-16) — the
# skill_error_reports retirement from #471. If a sibling migration takes a child
# of paywall0925_hits before this merges (PR #468's ser1116_incident_issue_url is
# a candidate), this needs one line re-pinned or a merge revision.
down_revision = "ser1116b_retire_skill_err"
branch_labels = None
depends_on = None

TABLE = "incident_reports"
COLUMN = "report_type"
TYPE_INCIDENT = "incident"
TYPE_SKILL_ERROR = "skill_error"


def _is_empty_fingerprint(raw) -> bool:
    """True when the stored env_fingerprint holds no information.

    Handles every shape the JSON column can come back as across dialects: a real
    dict (Postgres / SQLAlchemy), a JSON string (SQLite stores JSON as text),
    NULL, or an empty string.
    """
    if raw is None:
        return True
    if isinstance(raw, dict):
        return len(raw) == 0
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return True
        try:
            parsed = json.loads(text)
        except ValueError:
            # Unparseable is NOT empty — leave it classified as a crash report.
            return False
        return isinstance(parsed, dict) and len(parsed) == 0
    # A list/scalar JSON value carries information, so it is not "empty".
    return False


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column(COLUMN, sa.String(32), nullable=False, server_default=TYPE_INCIDENT),
    )
    op.create_index(f"ix_{TABLE}_{COLUMN}", TABLE, [COLUMN])

    # ── backfill history ─────────────────────────────────────────────────────
    bind = op.get_bind()
    tbl = sa.table(
        TABLE,
        sa.column("id"),
        sa.column("command"),
        sa.column("exit_code"),
        sa.column("env_fingerprint"),
        # Must be declared here too: sa.update(tbl).values(report_type=...) raises
        # "Unconsumed column names" for any column the table object doesn't know,
        # and that only surfaces on a real dialect run — model-level tests never
        # execute this statement. Caught by the Postgres migration job, not by
        # anything faster.
        sa.column(COLUMN),
    )
    rows = bind.execute(sa.select(tbl.c.id, tbl.c.command, tbl.c.exit_code, tbl.c.env_fingerprint)).all()
    reclassified = 0
    for row in rows:
        if row.command is None and row.exit_code is None and _is_empty_fingerprint(row.env_fingerprint):
            bind.execute(sa.update(tbl).where(tbl.c.id == row.id).values(**{COLUMN: TYPE_SKILL_ERROR}))
            reclassified += 1
    # Surfaced, not swallowed: whoever runs this should be able to see how much of
    # the historical tail was inferred from shape rather than recorded by a writer.
    print(
        f"[ser1116c] {reclassified}/{len(rows)} incident_reports rows backfilled to "
        f"'{TYPE_SKILL_ERROR}' by row shape; the rest keep the '{TYPE_INCIDENT}' "
        "server_default. Rows written from here on are classified by their writer, "
        "not inferred."
    )


def downgrade() -> None:
    op.drop_index(f"ix_{TABLE}_{COLUMN}", table_name=TABLE)
    op.drop_column(TABLE, COLUMN)

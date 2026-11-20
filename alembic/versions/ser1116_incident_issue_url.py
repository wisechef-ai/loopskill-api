"""ser1116 — incident_reports.issue_url + feedback_status (skill-error write-back)

Both skill-error writers (app/mcp/tools/skill_error.py, app/skill_error_routes.py)
persist an **IncidentReport** and hand its id to the Feedback Dispatcher workflow,
which then PATCHes the created GitHub issue's URL to
/api/internal/feedback/{id}/issue-url. The workflow named table="feedback", so the
handler looked the id up in feedback_submissions, matched nothing, and answered 404
on 100% of skill-error dispatches — confirmed in prod journalctl for the two
2026-11-13/14 humanizer reports (GitHub #462, #463). incident_reports had no column
capable of holding the URL either, so the write was unsatisfiable at both ends.

Adds the two columns the write-back needs. Both nullable/defaulted so existing rows
stay valid, including rows from /api/feedback/incident which files no issue at all.

DOWNGRADE: drop the two columns.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "ser1116_incident_issue_url"
down_revision = "t4e706c5d_telemetry_uuid_fk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("incident_reports", sa.Column("issue_url", sa.Text(), nullable=True))
    op.add_column(
        "incident_reports",
        sa.Column("feedback_status", sa.Text(), nullable=True, server_default="pending"),
    )
    op.execute("UPDATE incident_reports SET feedback_status = 'pending' WHERE feedback_status IS NULL")


def downgrade() -> None:
    op.drop_column("incident_reports", "feedback_status")
    op.drop_column("incident_reports", "issue_url")

"""activate_0701 Phase E — EU data residency gate.

Server-side fail-closed residency enforcement. EU-resident fleets (residency="eu")
cannot receive non-EU-tagged connectors or composite loops with derived non-EU
residency. Praga (non-EU) is NOT onboarded this sprint — the gate ships ahead.
"""

revision = "act0701_e_residency"
down_revision = "act0701_ten_tenancy"
branch_labels = None
depends_on = None

from alembic import op  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402


def upgrade() -> None:
    op.add_column(
        "fleets",
        op.Column("residency", op.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("fleets", "residency")

"""activate_0701 Phase E — EU data residency gate.

Revision ID: act0701_e_residency
Revises: act0701_ten_tenancy
"""

revision = "act0701_e_residency"
down_revision = "act0701_ten_tenancy"
branch_labels = None
depends_on = None

from alembic import op  # noqa: E402
import sqlalchemy as sa  # noqa: E402


def upgrade() -> None:
    op.add_column("fleets", sa.Column("residency", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("fleets", "residency")

"""intent_survey_responses table (stabilization_2605 phase A)

Revision ID: d7a3c1f9e201
Revises: f1a2c3d4e5b6
Create Date: 2026-05-03 17:45:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision: str = "d7a3c1f9e201"
down_revision: Union[str, None] = "d1f7e2a4b9c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "intent_survey_responses",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("q1", sa.String(16), nullable=False),
        sa.Column("q2", sa.Text, nullable=True),
        sa.Column("q3", sa.Text, nullable=True),
        sa.Column("q4", sa.String(32), nullable=False),
        sa.Column("q5", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_intent_survey_responses_q1", "intent_survey_responses", ["q1"])
    op.create_index("ix_intent_survey_responses_q4", "intent_survey_responses", ["q4"])
    op.create_index("ix_intent_survey_responses_created_at", "intent_survey_responses", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_intent_survey_responses_created_at", table_name="intent_survey_responses")
    op.drop_index("ix_intent_survey_responses_q4", table_name="intent_survey_responses")
    op.drop_index("ix_intent_survey_responses_q1", table_name="intent_survey_responses")
    op.drop_table("intent_survey_responses")

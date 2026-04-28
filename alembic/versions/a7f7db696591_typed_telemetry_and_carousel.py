"""typed_telemetry_and_carousel

Revision ID: a7f7db696591
Revises: 4ba0bf05cd47
Create Date: 2026-04-28 20:54:32.618982

ADDITIVE ONLY — per Sprint 4 contract invariants.
  - Never DROP columns (on production).
  - Never ALTER TYPE or RENAME.
  - All new columns have server/Python-side defaults so existing rows are valid.

Columns added
-------------
telemetry_events:
  skill_id          VARCHAR(36) nullable            (FK to skills.id, stored as UUID string)
  goal_class        VARCHAR(64) nullable            (client-reporting, social-posting, …)
  duration_seconds  INTEGER nullable                (0..86400)
  retry_count       INTEGER nullable default 0
  user_intervention BOOLEAN nullable default False
  agent_class_hash  VARCHAR(64) nullable            (hex 8–64 chars)

carousel_entries:
  role              VARCHAR(64) nullable            (new-capability, replaces, experimental)
  score             FLOAT nullable                  (scoring algo output, 0..10)

skills:
  vertical          VARCHAR(64) nullable            (agency, solo, enterprise, …)
  rating_avg        FLOAT nullable                  (0..5, default 3.0 in scoring algo)
  install_count     INTEGER not-null default 0      (denormalised counter for scoring)
  is_free           BOOLEAN nullable                (pricing tier flag used by carousel filter)
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a7f7db696591'
down_revision: Union[str, Sequence[str], None] = '4ba0bf05cd47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add typed-telemetry columns and carousel scoring columns (additive only).

    Uses op.add_column() directly — no table reflection needed, so this works
    cleanly on both PostgreSQL (production) and SQLite (tests).
    """

    # ------------------------------------------------------------------
    # telemetry_events — typed structured columns
    # ------------------------------------------------------------------
    op.add_column(
        'telemetry_events',
        sa.Column('skill_id', sa.String(36), nullable=True,
                  comment='FK to skills.id (UUID; stored as string for SQLite compat)')
    )
    op.add_column(
        'telemetry_events',
        sa.Column('goal_class', sa.String(64), nullable=True,
                  comment='client-reporting | social-posting | seo-audit | proposal | agent-rescue | other')
    )
    op.add_column(
        'telemetry_events',
        sa.Column('duration_seconds', sa.Integer(), nullable=True,
                  comment='0..86400')
    )
    op.add_column(
        'telemetry_events',
        sa.Column('retry_count', sa.Integer(), nullable=True,
                  server_default='0',
                  comment='Number of retries before success/failure')
    )
    op.add_column(
        'telemetry_events',
        sa.Column('user_intervention', sa.Boolean(), nullable=True,
                  server_default=sa.false(),
                  comment='Was human input required?')
    )
    op.add_column(
        'telemetry_events',
        sa.Column('agent_class_hash', sa.String(64), nullable=True,
                  comment='hex 8-64 chars identifying agent class client-side')
    )
    op.add_column(
        'telemetry_events',
        sa.Column('install_event_id', sa.String(36), nullable=True,
                  comment='FK to install_events.id (UUID; stored as string for SQLite compat)')
    )

    # ------------------------------------------------------------------
    # carousel_entries — scoring output columns
    # ------------------------------------------------------------------
    op.add_column(
        'carousel_entries',
        sa.Column('slot', sa.Integer(), nullable=True,
                  comment='1-indexed slot in today\'s carousel (1..7)')
    )
    op.add_column(
        'carousel_entries',
        sa.Column('role', sa.String(64), nullable=True,
                  comment='new-capability | replaces | experimental')
    )
    op.add_column(
        'carousel_entries',
        sa.Column('verdict', sa.String(32), nullable=True,
                  comment='promote | hold | archive — set by verdict cron')
    )
    op.add_column(
        'carousel_entries',
        sa.Column('score', sa.Float(), nullable=True,
                  comment='Scoring algo output (0..10)')
    )

    # Unique index on (featured_date, slot) to prevent race-condition duplicate inserts
    op.create_index(
        'uq_carousel_featured_date_slot',
        'carousel_entries',
        ['featured_date', 'slot'],
        unique=True,
    )

    # ------------------------------------------------------------------
    # skills — fields used by carousel scoring algorithm
    # ------------------------------------------------------------------
    op.add_column(
        'skills',
        sa.Column('vertical', sa.String(64), nullable=True,
                  comment='agency | solo | enterprise | horizontal')
    )
    op.add_column(
        'skills',
        sa.Column('rating_avg', sa.Float(), nullable=True,
                  comment='Average user rating 0..5; scoring defaults to 3.0 when NULL')
    )
    op.add_column(
        'skills',
        sa.Column('install_count', sa.Integer(), nullable=False,
                  server_default='0',
                  comment='Denormalised install counter for scoring algorithm')
    )
    op.add_column(
        'skills',
        sa.Column('is_free', sa.Boolean(), nullable=True,
                  comment='Pricing flag used by carousel public filter')
    )


def downgrade() -> None:
    """Remove the columns added in this revision.

    NOTE: Downgrade is provided for local dev / CI rollback only.
    It is NEVER run on production (see Sprint 4 contract — destructive ops
    are prohibited on prod).  SQLite requires batch mode for DROP COLUMN.
    """
    with op.batch_alter_table('skills') as batch_op:
        batch_op.drop_column('is_free')
        batch_op.drop_column('install_count')
        batch_op.drop_column('rating_avg')
        batch_op.drop_column('vertical')

    with op.batch_alter_table('carousel_entries') as batch_op:
        batch_op.drop_index('uq_carousel_featured_date_slot')
        batch_op.drop_column('score')
        batch_op.drop_column('verdict')
        batch_op.drop_column('role')
        batch_op.drop_column('slot')

    with op.batch_alter_table('telemetry_events') as batch_op:
        batch_op.drop_column('install_event_id')
        batch_op.drop_column('agent_class_hash')
        batch_op.drop_column('user_intervention')
        batch_op.drop_column('retry_count')
        batch_op.drop_column('duration_seconds')
        batch_op.drop_column('goal_class')
        batch_op.drop_column('skill_id')

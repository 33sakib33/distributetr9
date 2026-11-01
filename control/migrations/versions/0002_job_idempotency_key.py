"""job idempotency key

Revision ID: 0002
Revises: 0001
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('jobs', sa.Column('idempotency_key', sa.Text(), nullable=True))
    op.create_index('uq_jobs_user_idempotency_key', 'jobs', ['user_id', 'idempotency_key'], unique=True, postgresql_where=sa.text('idempotency_key IS NOT NULL'))


def downgrade() -> None:
    op.drop_index('uq_jobs_user_idempotency_key', table_name='jobs', postgresql_where=sa.text('idempotency_key IS NOT NULL'))
    op.drop_column('jobs', 'idempotency_key')

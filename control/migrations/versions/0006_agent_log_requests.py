"""agent log requests

Revision ID: 0006
Revises: 0005
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('nodes', sa.Column('logs_requested_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('nodes', sa.Column('logs_request_sent_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('nodes', sa.Column('logs_uploaded_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('nodes', sa.Column('logs_object_key', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('nodes', 'logs_object_key')
    op.drop_column('nodes', 'logs_uploaded_at')
    op.drop_column('nodes', 'logs_request_sent_at')
    op.drop_column('nodes', 'logs_requested_at')

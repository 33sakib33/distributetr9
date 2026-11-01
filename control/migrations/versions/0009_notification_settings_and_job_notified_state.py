"""notification settings and job notified state

Revision ID: 0009
Revises: 0008
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('jobs', sa.Column('notified_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('jobs', sa.Column('notify_tries', sa.Integer(), server_default=sa.text('0'), nullable=False))
    op.add_column('users', sa.Column('notify_email', sa.Boolean(), server_default=sa.text('true'), nullable=False))
    op.add_column('users', sa.Column('notify_webhook_url', sa.Text(), nullable=True))
    # Jobs that finished before this release are not announced.
    op.execute("UPDATE jobs SET notified_at = now() WHERE completed_at IS NOT NULL")


def downgrade() -> None:
    op.drop_column('users', 'notify_webhook_url')
    op.drop_column('users', 'notify_email')
    op.drop_column('jobs', 'notify_tries')
    op.drop_column('jobs', 'notified_at')

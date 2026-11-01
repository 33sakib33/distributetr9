"""alerts table, node decommission and rooms

Revision ID: 0007
Revises: 0006
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('alerts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('severity', sa.String(length=16), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('first_seen', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen', sa.DateTime(timezone=True), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('acknowledged_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('acknowledged_by', sa.Text(), nullable=True),
    sa.Column('muted_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('notified_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key')
    )
    op.add_column('nodes', sa.Column('decommissioned_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('nodes', sa.Column('room', sa.Text(), nullable=True))
    op.add_column('nodes', sa.Column('room_source', sa.String(length=16), nullable=True))
    op.add_column('nodes', sa.Column('labels', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False))


def downgrade() -> None:
    op.drop_column('nodes', 'labels')
    op.drop_column('nodes', 'room_source')
    op.drop_column('nodes', 'room')
    op.drop_column('nodes', 'decommissioned_at')
    op.drop_table('alerts')

"""schedules, platform settings, audit log and retired environments

Revision ID: 0008
Revises: 0007
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('audit_log',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('actor_user_id', sa.Uuid(), nullable=True),
    sa.Column('actor_email', sa.Text(), nullable=False),
    sa.Column('action', sa.Text(), nullable=False),
    sa.Column('target_type', sa.Text(), nullable=False),
    sa.Column('target_id', sa.Text(), nullable=True),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_audit_log_at', 'audit_log', ['at'], unique=False)
    op.create_table('platform_settings',
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('value', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_by', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('availability_windows',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('room', sa.Text(), nullable=True),
    sa.Column('node_id', sa.Uuid(), nullable=True),
    sa.Column('weekday', sa.Integer(), nullable=False),
    sa.Column('start_minute', sa.Integer(), nullable=False),
    sa.Column('end_minute', sa.Integer(), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.add_column('environments', sa.Column('retired_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('environments', 'retired_at')
    op.drop_table('availability_windows')
    op.drop_table('platform_settings')
    op.drop_index('ix_audit_log_at', table_name='audit_log')
    op.drop_table('audit_log')

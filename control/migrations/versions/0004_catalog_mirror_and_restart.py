"""catalog mirror builds and restart reasons

Revision ID: 0004
Revises: 0003
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('node_events',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('node_id', sa.Uuid(), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_node_events_node_at', 'node_events', ['node_id', 'at'], unique=False)
    op.add_column('environment_builds', sa.Column('kind', sa.String(length=16), server_default=sa.text("'pip'"), nullable=False))
    op.add_column('environment_builds', sa.Column('environment_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('environment_builds_environment_id_fkey', 'environment_builds', 'environments', ['environment_id'], ['id'])
    op.add_column('environments', sa.Column('upstream_image', sa.Text(), nullable=True))
    op.add_column('nodes', sa.Column('last_restart', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column('nodes', 'last_restart')
    op.drop_column('environments', 'upstream_image')
    op.drop_constraint('environment_builds_environment_id_fkey', 'environment_builds', type_='foreignkey')
    op.drop_column('environment_builds', 'environment_id')
    op.drop_column('environment_builds', 'kind')
    op.drop_index('ix_node_events_node_at', table_name='node_events')
    op.drop_table('node_events')

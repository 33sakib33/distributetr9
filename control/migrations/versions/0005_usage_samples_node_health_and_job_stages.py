"""usage samples, node health and job stages

Revision ID: 0005
Revises: 0004
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('node_health',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('node_id', sa.Uuid(), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('gpu_temp_c', sa.Float(), nullable=True),
    sa.Column('gpu_pct', sa.Float(), nullable=True),
    sa.Column('disk_free_bytes', sa.BigInteger(), nullable=True),
    sa.Column('busy', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_node_health_node_at', 'node_health', ['node_id', 'at'], unique=False)
    op.create_table('attempt_usage',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('attempt_id', sa.Uuid(), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('gpu_pct', sa.Float(), nullable=True),
    sa.Column('vram_mb', sa.Integer(), nullable=True),
    sa.Column('cpu_pct', sa.Float(), nullable=True),
    sa.Column('ram_mb', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['attempt_id'], ['job_attempts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_attempt_usage_attempt_at', 'attempt_usage', ['attempt_id', 'at'], unique=False)
    op.add_column('job_attempts', sa.Column('stages', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('job_attempts', sa.Column('peak_vram_mb', sa.Integer(), nullable=True))
    op.add_column('job_attempts', sa.Column('peak_ram_mb', sa.Integer(), nullable=True))
    op.add_column('job_attempts', sa.Column('avg_gpu_pct', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('job_attempts', 'avg_gpu_pct')
    op.drop_column('job_attempts', 'peak_ram_mb')
    op.drop_column('job_attempts', 'peak_vram_mb')
    op.drop_column('job_attempts', 'stages')
    op.drop_index('ix_attempt_usage_attempt_at', table_name='attempt_usage')
    op.drop_table('attempt_usage')
    op.drop_index('ix_node_health_node_at', table_name='node_health')
    op.drop_table('node_health')

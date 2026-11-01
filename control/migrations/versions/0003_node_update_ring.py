"""node update ring

Revision ID: 0003
Revises: 0002
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('nodes', sa.Column('update_ring', sa.Integer(), server_default=sa.text('2'), nullable=False))


def downgrade() -> None:
    op.drop_column('nodes', 'update_ring')

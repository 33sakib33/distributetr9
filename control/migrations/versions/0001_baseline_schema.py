"""baseline schema

Revision ID: 0001
Revises: 
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('environment_builds',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('environment_hash', sa.Text(), nullable=False),
    sa.Column('state', sa.String(length=32), nullable=False),
    sa.Column('base_image', sa.Text(), nullable=False),
    sa.Column('spec', sa.Text(), nullable=True),
    sa.Column('compat_class', sa.Text(), nullable=True),
    sa.Column('lockfile', sa.Text(), nullable=True),
    sa.Column('image_digest', sa.Text(), nullable=True),
    sa.Column('checksum', sa.Text(), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('claimed_node_id', sa.Uuid(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('environment_hash')
    )
    op.create_table('environments',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('framework', sa.Text(), nullable=False),
    sa.Column('python_version', sa.Text(), nullable=False),
    sa.Column('cuda_version', sa.Text(), nullable=False),
    sa.Column('gpu_archs', postgresql.ARRAY(sa.Text()), nullable=False),
    sa.Column('image_digest', sa.Text(), nullable=False),
    sa.Column('lockfile', sa.Text(), nullable=True),
    sa.Column('cuda_preflight_ok', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('nodes',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('boot_session_id', sa.Text(), nullable=False),
    sa.Column('hostname', sa.Text(), nullable=False),
    sa.Column('gpu_model', sa.Text(), nullable=False),
    sa.Column('compatibility_class', sa.Text(), nullable=False),
    sa.Column('vram_total_mb', sa.Integer(), nullable=False),
    sa.Column('vram_allocatable_mb', sa.Integer(), nullable=False),
    sa.Column('compute_capability', sa.Text(), nullable=False),
    sa.Column('driver_version', sa.Text(), nullable=False),
    sa.Column('windows_version', sa.Text(), nullable=False),
    sa.Column('wsl_version', sa.Text(), nullable=False),
    sa.Column('agent_version', sa.Text(), nullable=False),
    sa.Column('cpu_cores', sa.Integer(), nullable=False),
    sa.Column('ram_mb', sa.Integer(), nullable=False),
    sa.Column('disk_free_bytes', sa.BigInteger(), nullable=False),
    sa.Column('last_heartbeat_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('eligibility', sa.String(length=32), nullable=False),
    sa.Column('eligibility_reason', sa.Text(), nullable=True),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('gpu_desktop_busy', sa.Boolean(), nullable=False),
    sa.Column('last_health', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('registered_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_nodes_eligibility_class', 'nodes', ['eligibility', 'compatibility_class'], unique=False)
    op.create_table('quotas',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('subject_type', sa.String(length=16), nullable=False),
    sa.Column('subject_id', sa.Uuid(), nullable=False),
    sa.Column('max_concurrent_jobs', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('subject_type', 'subject_id', name='uq_quota_subject')
    )
    op.create_table('users',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('university_sub', sa.Text(), nullable=False),
    sa.Column('email', sa.Text(), nullable=False),
    sa.Column('display_name', sa.Text(), nullable=False),
    sa.Column('is_admin', sa.Boolean(), nullable=False),
    sa.Column('disabled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('university_sub')
    )
    op.create_table('node_credentials',
    sa.Column('node_id', sa.Uuid(), nullable=False),
    sa.Column('token_hash', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], ),
    sa.PrimaryKeyConstraint('node_id')
    )
    op.create_table('projects',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('created_by', 'name', name='uq_project_owner_name')
    )
    op.create_table('jobs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('project_id', sa.Uuid(), nullable=False),
    sa.Column('submission_artifact_id', sa.Uuid(), nullable=True),
    sa.Column('entrypoint', sa.Text(), nullable=False),
    sa.Column('args', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('environment_id', sa.Uuid(), nullable=False),
    sa.Column('resources', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('max_attempts', sa.Integer(), nullable=False),
    sa.Column('max_runtime_seconds', sa.Integer(), nullable=False),
    sa.Column('desired_state', sa.String(length=16), nullable=False),
    sa.Column('accepted_attempt_id', sa.Uuid(), nullable=True),
    sa.Column('pending_reason', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['environment_id'], ['environments.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_jobs_desired_created', 'jobs', ['desired_state', 'created_at'], unique=False, postgresql_where=sa.text('accepted_attempt_id IS NULL'))
    op.create_index('ix_jobs_project_created', 'jobs', ['project_id', 'created_at'], unique=False)
    op.create_index('ix_jobs_user_created', 'jobs', ['user_id', 'created_at'], unique=False)
    op.create_table('project_members',
    sa.Column('project_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('project_id', 'user_id')
    )
    op.create_table('job_attempts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('job_id', sa.Uuid(), nullable=False),
    sa.Column('attempt_number', sa.Integer(), nullable=False),
    sa.Column('node_id', sa.Uuid(), nullable=False),
    sa.Column('fencing_token', sa.Uuid(), nullable=False),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('observed_state', sa.String(length=32), nullable=False),
    sa.Column('exit_code', sa.Integer(), nullable=True),
    sa.Column('failure_category', sa.String(length=32), nullable=True),
    sa.Column('output_namespace', sa.Text(), nullable=False),
    sa.Column('last_log_seq', sa.BigInteger(), nullable=False),
    sa.Column('log_bytes', sa.BigInteger(), nullable=False),
    sa.Column('resource_usage', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('completion_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('fencing_token'),
    sa.UniqueConstraint('job_id', 'attempt_number', name='uq_attempt_number')
    )
    op.create_index('ix_attempts_lease_active', 'job_attempts', ['lease_expires_at'], unique=False, postgresql_where=sa.text("observed_state IN ('leased','starting','running','uploading','cancelling','unknown')"))
    op.create_index('ix_attempts_node_state', 'job_attempts', ['node_id', 'observed_state'], unique=False)
    op.create_index('uq_attempts_one_active_per_node', 'job_attempts', ['node_id'], unique=True, postgresql_where=sa.text("observed_state IN ('leased','starting','running','uploading','cancelling','unknown')"))
    op.create_table('artifacts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('job_id', sa.Uuid(), nullable=True),
    sa.Column('attempt_id', sa.Uuid(), nullable=True),
    sa.Column('build_id', sa.Uuid(), nullable=True),
    sa.Column('type', sa.String(length=32), nullable=False),
    sa.Column('object_key', sa.Text(), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('checksum_sha256', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['attempt_id'], ['job_attempts.id'], ),
    sa.ForeignKeyConstraint(['build_id'], ['environment_builds.id'], ),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('object_key')
    )
    op.create_index('ix_artifacts_job_type', 'artifacts', ['job_id', 'type'], unique=False)
    # jobs <-> job_attempts / artifacts reference each other, so these come last.
    op.create_foreign_key('fk_jobs_accepted_attempt', 'jobs', 'job_attempts', ['accepted_attempt_id'], ['id'])
    op.create_foreign_key('fk_jobs_submission_artifact', 'jobs', 'artifacts', ['submission_artifact_id'], ['id'])


def downgrade() -> None:
    op.drop_constraint('fk_jobs_submission_artifact', 'jobs', type_='foreignkey')
    op.drop_constraint('fk_jobs_accepted_attempt', 'jobs', type_='foreignkey')
    op.drop_index('ix_artifacts_job_type', table_name='artifacts')
    op.drop_table('artifacts')
    op.drop_index('uq_attempts_one_active_per_node', table_name='job_attempts', postgresql_where=sa.text("observed_state IN ('leased','starting','running','uploading','cancelling','unknown')"))
    op.drop_index('ix_attempts_node_state', table_name='job_attempts')
    op.drop_index('ix_attempts_lease_active', table_name='job_attempts', postgresql_where=sa.text("observed_state IN ('leased','starting','running','uploading','cancelling','unknown')"))
    op.drop_table('job_attempts')
    op.drop_table('project_members')
    op.drop_index('ix_jobs_user_created', table_name='jobs')
    op.drop_index('ix_jobs_project_created', table_name='jobs')
    op.drop_index('ix_jobs_desired_created', table_name='jobs', postgresql_where=sa.text('accepted_attempt_id IS NULL'))
    op.drop_table('jobs')
    op.drop_table('projects')
    op.drop_table('node_credentials')
    op.drop_table('users')
    op.drop_table('quotas')
    op.drop_index('ix_nodes_eligibility_class', table_name='nodes')
    op.drop_table('nodes')
    op.drop_table('environments')
    op.drop_table('environment_builds')

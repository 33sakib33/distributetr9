from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def new_id() -> uuid.UUID:
    return uuid.uuid4()


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    university_sub: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("created_by", "name", name="uq_project_owner_name"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProjectMember(Base):
    __tablename__ = "project_members"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)


class Quota(Base):
    __tablename__ = "quotas"
    __table_args__ = (UniqueConstraint("subject_type", "subject_id", name="uq_quota_subject"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    max_concurrent_jobs: Mapped[int] = mapped_column(Integer, nullable=False)


class Node(Base):
    __tablename__ = "nodes"
    __table_args__ = (Index("ix_nodes_eligibility_class", "eligibility", "compatibility_class"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    boot_session_id: Mapped[str] = mapped_column(Text, nullable=False)
    hostname: Mapped[str] = mapped_column(Text, nullable=False)
    gpu_model: Mapped[str] = mapped_column(Text, nullable=False)
    compatibility_class: Mapped[str] = mapped_column(Text, nullable=False)
    vram_total_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    vram_allocatable_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    compute_capability: Mapped[str] = mapped_column(Text, nullable=False)
    driver_version: Mapped[str] = mapped_column(Text, nullable=False)
    windows_version: Mapped[str] = mapped_column(Text, nullable=False)
    wsl_version: Mapped[str] = mapped_column(Text, nullable=False)
    agent_version: Mapped[str] = mapped_column(Text, nullable=False)
    cpu_cores: Mapped[int] = mapped_column(Integer, nullable=False)
    ram_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    disk_free_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    eligibility: Mapped[str] = mapped_column(String(32), nullable=False, default="eligible")
    eligibility_reason: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="train")
    update_ring: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("2"))
    gpu_desktop_busy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_health: Mapped[dict | None] = mapped_column(JSONB)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class NodeCredential(Base):
    __tablename__ = "node_credentials"

    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id"), primary_key=True)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Environment(Base):
    __tablename__ = "environments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    framework: Mapped[str] = mapped_column(Text, nullable=False)
    python_version: Mapped[str] = mapped_column(Text, nullable=False)
    cuda_version: Mapped[str] = mapped_column(Text, nullable=False)
    gpu_archs: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    image_digest: Mapped[str] = mapped_column(Text, nullable=False)
    lockfile: Mapped[str | None] = mapped_column(Text)
    cuda_preflight_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EnvironmentBuild(Base):
    __tablename__ = "environment_builds"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    environment_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    base_image: Mapped[str] = mapped_column(Text, nullable=False)
    spec: Mapped[str | None] = mapped_column(Text)
    compat_class: Mapped[str | None] = mapped_column(Text)
    lockfile: Mapped[str | None] = mapped_column(Text)
    image_digest: Mapped[str | None] = mapped_column(Text)
    checksum: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    claimed_node_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_project_created", "project_id", "created_at"),
        Index("ix_jobs_user_created", "user_id", "created_at"),
        Index(
            "ix_jobs_desired_created",
            "desired_state",
            "created_at",
            postgresql_where=text("accepted_attempt_id IS NULL"),
        ),
        Index(
            "uq_jobs_user_idempotency_key",
            "user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), nullable=False)
    submission_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("artifacts.id", use_alter=True, name="fk_jobs_submission_artifact")
    )
    entrypoint: Mapped[str] = mapped_column(Text, nullable=False)
    args: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    environment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("environments.id"), nullable=False)
    resources: Mapped[dict] = mapped_column(JSONB, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    max_runtime_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    desired_state: Mapped[str] = mapped_column(String(16), nullable=False, default="run")
    accepted_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("job_attempts.id", use_alter=True, name="fk_jobs_accepted_attempt")
    )
    pending_reason: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    attempts: Mapped[list[JobAttempt]] = relationship(
        back_populates="job",
        foreign_keys="JobAttempt.job_id",
        order_by="JobAttempt.attempt_number",
    )


class JobAttempt(Base):
    __tablename__ = "job_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number", name="uq_attempt_number"),
        Index("ix_attempts_node_state", "node_id", "observed_state"),
        Index(
            "uq_attempts_one_active_per_node",
            "node_id",
            unique=True,
            postgresql_where=text(
                "observed_state IN "
                "('leased','starting','running','uploading','cancelling','unknown')"
            ),
        ),
        Index(
            "ix_attempts_lease_active",
            "lease_expires_at",
            postgresql_where=text(
                "observed_state IN "
                "('leased','starting','running','uploading','cancelling','unknown')"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id"), nullable=False)
    fencing_token: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True, nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_state: Mapped[str] = mapped_column(String(32), nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    failure_category: Mapped[str | None] = mapped_column(String(32))
    output_namespace: Mapped[str] = mapped_column(Text, nullable=False)
    last_log_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    log_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    resource_usage: Mapped[dict | None] = mapped_column(JSONB)
    completion_payload: Mapped[dict | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    job: Mapped[Job] = relationship(back_populates="attempts", foreign_keys=[job_id])


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (Index("ix_artifacts_job_type", "job_id", "type"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id"))
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("job_attempts.id"))
    build_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("environment_builds.id"))
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    object_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

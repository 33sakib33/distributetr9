from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
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
    # Job-finished notifications (B26): e-mail on by default, webhook only when set.
    notify_email: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    notify_webhook_url: Mapped[str | None] = mapped_column(Text)
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
    last_restart: Mapped[dict | None] = mapped_column(JSONB)
    # Forgotten PCs keep their history but leave the fleet, the scheduler, alerts and metrics (B15).
    decommissioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Where the PC sits (B16). `room_source` says who set it: the agent's config or an admin.
    room: Mapped[str | None] = mapped_column(Text)
    room_source: Mapped[str | None] = mapped_column(String(16))
    labels: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    # Agent log on demand (B13): an admin asks, the next heartbeat carries the
    # request, the agent uploads its log tail to `logs_object_key`.
    logs_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    logs_request_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    logs_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    logs_object_key: Mapped[str | None] = mapped_column(Text)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AlertRecord(Base):
    """One alert key and its lifecycle: open, acknowledged or muted, resolved, re-opened."""

    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[str | None] = mapped_column(Text)
    muted_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the webhook last announced it; reset when the alert re-opens.
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AvailabilityWindow(Base):
    """A weekly window in LAB_TIMEZONE when a room or one PC is closed to jobs
    (class hours, B17). Exactly one of `room` and `node_id` is set."""

    __tablename__ = "availability_windows"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    room: Mapped[str | None] = mapped_column(Text)
    node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("nodes.id"))
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)  # Monday = 0
    start_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    end_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)


class PlatformSetting(Base):
    """Operator-editable settings that override the environment defaults (B20)."""

    __tablename__ = "platform_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[object] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_by: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base):
    """Who did what to what, for every admin mutation (B21)."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_at", "at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    actor_email: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict | None] = mapped_column(JSONB)


class NodeEvent(Base):
    __tablename__ = "node_events"
    __table_args__ = (Index("ix_node_events_node_at", "node_id", "at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id"), nullable=False)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB)


class NodeHealth(Base):
    """One throttled heartbeat sample per node (at most one a minute)."""

    __tablename__ = "node_health"
    __table_args__ = (Index("ix_node_health_node_at", "node_id", "at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id"), nullable=False)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    gpu_temp_c: Mapped[float | None] = mapped_column(Float)
    gpu_pct: Mapped[float | None] = mapped_column(Float)
    disk_free_bytes: Mapped[int | None] = mapped_column(BigInteger)
    busy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


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
    # The public image a catalog row was mirrored from (see envbuild mirror builds).
    upstream_image: Mapped[str | None] = mapped_column(Text)
    # A retired catalog image is hidden and can not be chosen; jobs already using it keep running (B19).
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EnvironmentBuild(Base):
    __tablename__ = "environment_builds"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    environment_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    # "pip" builds a custom environment; "mirror" copies a catalog image into the lab registry.
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pip", server_default=text("'pip'")
    )
    environment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("environments.id"))
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
    # When the owner was told the job finished; `notify_tries` counts failed deliveries.
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notify_tries: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))

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
    # {"<stage>": {"started_at", "finished_at", "progress"}} reported by the agent (see telemetry.py).
    stages: Mapped[dict | None] = mapped_column(JSONB)
    peak_vram_mb: Mapped[int | None] = mapped_column(Integer)
    peak_ram_mb: Mapped[int | None] = mapped_column(Integer)
    avg_gpu_pct: Mapped[float | None] = mapped_column(Float)
    completion_payload: Mapped[dict | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    job: Mapped[Job] = relationship(back_populates="attempts", foreign_keys=[job_id])


class AttemptUsage(Base):
    """A resource sample the agent sent with a lease renewal."""

    __tablename__ = "attempt_usage"
    __table_args__ = (Index("ix_attempt_usage_attempt_at", "attempt_id", "at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    attempt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("job_attempts.id"), nullable=False)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    gpu_pct: Mapped[float | None] = mapped_column(Float)
    vram_mb: Mapped[int | None] = mapped_column(Integer)
    cpu_pct: Mapped[float | None] = mapped_column(Float)
    ram_mb: Mapped[int | None] = mapped_column(Integer)


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

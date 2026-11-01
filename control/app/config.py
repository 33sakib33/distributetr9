from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # "production" turns dev conveniences into startup errors (see production_problems).
    environment: str = "dev"

    database_url: str = "postgresql+asyncpg://lab:lab@localhost:5432/lab"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "labminio"
    minio_secret_key: str = "labminio"
    minio_bucket: str = "lab"
    minio_secure: bool = False
    public_base_url: str = "http://127.0.0.1:8080"
    portal_base_url: str = "http://127.0.0.1:3000"
    registry_url: str = ""
    # With REGISTRY_URL set, copy catalog images into the lab registry (mirror builds).
    mirror_catalog_images: bool = True

    dev_auth: bool = True
    dev_auth_admin: bool = True
    admin_emails: str = "admin@uni.edu"
    oidc_allowed_email_domains: str = ""

    oidc_issuer: str = ""
    oidc_client_id: str = "lab-portal"
    oidc_client_secret: str = "lab-oidc-dev-secret"
    oidc_redirect_uri: str = "http://127.0.0.1:8080/auth/callback"
    oidc_scopes: str = "openid email profile"
    oidc_dev: bool = True
    session_hours: int = 12
    session_secure: bool = False

    # GET /availability lists only classes with a train PC heard from this recently.
    availability_class_max_age_days: int = 7

    lease_ttl_seconds: int = 30
    heartbeat_interval_seconds: int = 10
    disconnect_grace_seconds: int = 60
    disk_watermark_bytes: int = 1_073_741_824
    token_pepper: str = "dev-token-pepper-change-me"

    max_upload_bytes: int = 524_288_000
    global_max_runtime_seconds: int = 86_400
    default_user_concurrency: int = 2
    default_project_concurrency: int = 4
    log_lines_per_second: int = 200
    log_bytes_per_attempt: int = 32 * 1024 * 1024
    log_chunk_lines: int = 500
    output_bytes_per_attempt: int = 5 * 1024 * 1024 * 1024
    output_file_bytes: int = 2 * 1024 * 1024 * 1024
    cancel_grace_seconds: int = 15
    claim_poll_seconds: int = 25
    claim_candidate_limit: int = 200
    runtime_grace_seconds: int = 120

    # Agent self-update. Empty target = the version in agent/agent.py. Nodes
    # with update_ring <= agent_rollout_ring get it (0 canary, 1 early, 2 all).
    agent_dir: str = ""
    # Where scripts/ and supervisor/ live for the PC install package (repo root by default).
    install_root: str = ""
    agent_signing_key_path: str = ""
    agent_target_version: str = ""
    agent_min_version: str = ""
    agent_rollout_ring: int = 2
    default_update_ring: int = 2
    alert_webhook_url: str = ""
    # With a token set, GET /metrics needs "Authorization: Bearer <token>".
    metrics_token: str = ""
    # Class-hour windows (availability schedules) are written in this IANA zone.
    lab_timezone: str = "UTC"
    # Job-finished e-mail goes out through this SMTP server; empty SMTP_HOST turns e-mail off.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "lab@localhost"
    smtp_starttls: bool = True
    # How often the notification worker looks for finished jobs, and how often one job is tried.
    notify_check_seconds: int = 15
    notify_max_tries: int = 3
    # Webhooks may only reach public addresses (a user chooses the URL, the server
    # sends it); set 1 to allow a webhook receiver on the lab network.
    notify_allow_private_webhooks: bool = False
    # Rate limits (requests a minute per user, or per IP when nobody is signed in).
    rate_limit_enabled: bool = True
    rate_limit_jobs_per_minute: int = 30
    rate_limit_inspect_per_minute: int = 30
    rate_limit_login_per_minute: int = 10
    rate_limit_client_errors_per_minute: int = 20
    # "text" for a developer's terminal, "json" (one object per line) for the server.
    log_format: str = "text"
    alert_cooldown_seconds: int = 3600
    alert_check_seconds: int = 60
    alert_offline_seconds: int = 600
    alert_lease_losses_per_hour: int = 5
    alert_node_failures: int = 3
    alert_queue_age_seconds: int = 4 * 3600
    strict_node_class: bool = False
    high_vram_mb: int = 20_480
    high_vram_reserve_min: int = 16_384
    aging_steal_seconds: int = 900

    def production_problems(self) -> list[str]:
        """Settings that are fine for a laptop and dangerous on the lab server."""
        problems = []
        if self.dev_auth:
            problems.append("DEV_AUTH must be 0 (the X-Dev-User header lets anyone act as anyone)")
        if self.dev_auth_admin:
            problems.append("DEV_AUTH_ADMIN must be 0")
        if self.oidc_dev:
            problems.append("OIDC_DEV must be 0; configure the campus OIDC issuer")
        if not self.campus_login():
            problems.append("OIDC_ISSUER and OIDC_CLIENT_ID must point at the campus identity provider")
        if self.oidc_client_secret == "lab-oidc-dev-secret":
            problems.append("OIDC_CLIENT_SECRET is the dev default")
        if self.token_pepper in {"dev-token-pepper-change-me", ""} or len(self.token_pepper) < 24:
            problems.append("TOKEN_PEPPER must be a random secret of 24+ characters")
        if self.minio_secret_key in {"labminio", ""}:
            problems.append("MINIO_SECRET_KEY is the dev default")
        if ":lab@" in self.database_url:
            problems.append("DATABASE_URL uses the dev Postgres password")
        if not self.session_secure:
            problems.append("SESSION_SECURE must be 1 behind HTTPS")
        if not self.agent_signing_key_path:
            problems.append("AGENT_SIGNING_KEY_PATH must be on a persistent volume")
        return problems

    def campus_login(self) -> bool:
        issuer = (self.oidc_issuer or "").rstrip("/")
        if not issuer or not self.oidc_client_id:
            return False
        return not issuer.endswith("/oidc")

    def oidc_enabled(self) -> bool:
        return bool(self.campus_login() or self.oidc_dev)

    def issuer_url(self) -> str:
        if self.oidc_issuer:
            return self.oidc_issuer.rstrip("/")
        if self.oidc_dev:
            return "http://127.0.0.1:8080/oidc"
        return ""


settings = Settings()

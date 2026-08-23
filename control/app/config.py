from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://lab:lab@localhost:5432/lab"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "labminio"
    minio_secret_key: str = "labminio"
    minio_bucket: str = "lab"
    minio_secure: bool = False
    public_base_url: str = "http://127.0.0.1:8080"
    portal_base_url: str = "http://127.0.0.1:3000"
    registry_url: str = ""

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
    strict_node_class: bool = False
    high_vram_mb: int = 20_480
    high_vram_reserve_min: int = 16_384
    aging_steal_seconds: int = 900

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

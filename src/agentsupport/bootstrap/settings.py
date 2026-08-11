from pathlib import Path
from uuid import UUID

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for AgentSupport."""

    model_config = SettingsConfigDict(
        env_prefix="AGENTSUPPORT_",
        env_file=".env",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://agent:agent@localhost:5432/agentsupport"
    persistence_mode: str = "memory"
    auto_create_schema: bool = True
    execution_mode: str = "inline"
    instance_id: str = ""
    job_lease_seconds: int = 30
    job_heartbeat_seconds: int = 10
    worker_poll_interval_seconds: float = 0.25
    event_poll_interval_seconds: float = 0.25
    redis_url: str | None = None
    max_job_attempts: int = 3
    runtime_driver: str = "memory"
    runtime_context: str = "desktop-linux"
    runner_image: str = "agentsupport-runner:dev"
    kubernetes_api_server: str = "https://kubernetes.default.svc"
    kubernetes_namespace: str = "default"
    kubernetes_pvc_size: str = "10Gi"
    kubernetes_storage_class: str | None = None
    kubernetes_runner_secret_name: str | None = None
    core_runner_url: str | None = None
    core_runner_timeout_seconds: float = 300.0
    core_runner_workspace_root: Path | None = None
    workspace_root: Path = Path("workspace-data")
    waiting_input_timeout_seconds: int = 30 * 60
    pause_worker_interval_seconds: int = 5
    max_active_sessions: int = 2
    max_queued_conversations: int = 100
    runtime_start_timeout_seconds: int = 30
    runtime_stop_grace_seconds: int = 30
    health_check_interval_seconds: int = 5
    health_failure_threshold: int = 3
    skills_root: Path = Path("skills")
    enabled_skills: str = ""
    # API authentication contract: this platform intentionally ships without
    # token-based authentication. Only "none" is implemented; any other value
    # fails fast so a half-baked auth mode cannot silently reach production.
    api_auth_mode: str = "none"
    # Missing-precondition auto-completion (default ON): when a request refers
    # to a resource that does not exist yet, the platform creates it on demand.
    auto_create_missing: bool = True
    # Comma-separated scopes, or "all": organization,user,preset,project,workspace,session
    auto_create_scopes: str = "all"
    # Optional stable IDs used when a missing workspace/user must be created
    # and the request carries no explicit reference. When unset, stable
    # namespaced UUIDs are derived so repeated calls converge on one entity.
    default_workspace_id: UUID | None = None
    default_user_id: UUID | None = None
    auto_resource_name: str = "auto"
    # Retention windows for transient coordination state. Inline (in-memory)
    # mode prunes idempotency records and unreferenced checkpoints on the
    # background timer; PostgreSQL mode applies the same windows plus event,
    # job and outbox retention via the dedicated retention process. A window
    # of 0 disables that cleanup.
    retention_idempotency_hours: int = 24
    retention_unreferenced_checkpoints_hours: int = 24
    retention_cleanup_interval_hours: int = 24
    retention_published_outbox_days: int = 7
    retention_terminal_jobs_days: int = 30
    retention_terminal_events_days: int = 90

    @model_validator(mode="after")
    def _validate_auth_mode(self) -> "Settings":
        if self.api_auth_mode != "none":
            raise ValueError(
                "only api_auth_mode='none' is implemented; "
                "token-based authentication is intentionally not provided"
            )
        return self

    @property
    def auto_create_scopes_set(self) -> set[str]:
        raw = self.auto_create_scopes.strip()
        if raw == "all":
            return {"all"}
        return {item.strip() for item in raw.split(",") if item.strip()}


settings = Settings()

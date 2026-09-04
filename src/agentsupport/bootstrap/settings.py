from pathlib import Path

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
    execution_mode: str = "temporal"
    instance_id: str = ""
    event_poll_interval_seconds: float = 0.25
    redis_url: str | None = None
    core_runner_url: str | None = None
    core_runner_timeout_seconds: float = 300.0
    core_runner_workspace_root: Path | None = None
    #: Runner-side execution workspace root. When set, generation event files
    #: are written here instead of the control-plane workspace root, so an
    #: agent running against a non-mounted local workspace can read them.
    runner_workspace_root: Path | None = None
    workspace_root: Path = Path("workspace-data")
    # Temporal execution mode (phase 0 prototype). Requires a Temporal server
    # reachable at temporal_host and a shared persistence backend.
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "agentsupport"
    temporal_workflow_timeout_seconds: int = 6 * 60 * 60
    pause_worker_interval_seconds: int = 5
    max_active_sessions: int = 2
    max_queued_conversations: int = 100
    #: Default page size for list endpoints when the caller omits ``limit``.
    #: ``0`` disables the cap (full list). An explicit ``limit`` always wins.
    list_default_limit: int = 100
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
    auto_create_scopes: str = "workspace,session"
    # Optional stable IDs used when a missing workspace/user must be created
    # and the request carries no explicit reference. When unset, stable
    # namespaced UUIDs are derived so repeated calls converge on one entity.
    # Retention windows for transient coordination state. Inline (in-memory)
    # mode prunes idempotency records and unreferenced checkpoints on the
    # background timer; PostgreSQL mode applies the same windows plus event,
    # job and outbox retention via the dedicated retention process. A window
    # of 0 disables that cleanup.
    retention_idempotency_hours: int = 24
    retention_unreferenced_checkpoints_hours: int = 24
    retention_cleanup_interval_hours: int = 24
    retention_published_outbox_days: int = 7
    retention_lease_days: int = 30
    retention_terminal_events_days: int = 90
    # Runner self-registration. A non-empty runner_token enables the internal
    # /runners/* endpoints; heartbeats older than the timeout are expired by
    # the inline health worker or the runner heartbeat reconciler.
    runner_token: str = ""
    runner_heartbeat_timeout_seconds: float = 30.0
    # Evaluation layer: per-case wait limit when a case runs through Temporal.
    # A case that outlives this window is cancelled and recorded as ERROR.
    eval_case_timeout_seconds: int = 1800
    # Evaluation layer: auto-answer human gates. When enabled, a case whose
    # workflow parks at a waiting gate submits eval_auto_input instead of
    # timing out; eval_auto_answer_limit bounds repeated gates per case.
    eval_auto_interaction: bool = False
    eval_auto_input: str = "continue"
    eval_auto_answer_limit: int = 10
    # Observability: structured logging, tracing and metrics.
    log_format: str = "console"
    log_level: str = "INFO"
    service_name: str = "agentsupport"
    otel_exporter_otlp_endpoint: str = ""

    @model_validator(mode="after")
    def _validate_auth_mode(self) -> "Settings":
        if self.api_auth_mode != "none":
            raise ValueError(
                "only api_auth_mode='none' is implemented; "
                "token-based authentication is intentionally not provided"
            )
        if self.execution_mode != "temporal":
            raise ValueError(
                "only execution_mode='temporal' is supported; "
                "the inline runtime mode has been retired"
            )
        return self

    @property
    def auto_create_scopes_set(self) -> set[str]:
        raw = self.auto_create_scopes.strip()
        if raw == "all":
            return {"all"}
        return {item.strip() for item in raw.split(",") if item.strip()}


settings = Settings()

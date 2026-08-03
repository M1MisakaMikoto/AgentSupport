from pathlib import Path

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


settings = Settings()

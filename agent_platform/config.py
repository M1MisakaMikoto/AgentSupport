from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the single-organization first release."""

    model_config = SettingsConfigDict(env_prefix="AGENT_PLATFORM_", env_file=".env")

    database_url: str = "postgresql+psycopg://agent:agent@localhost:5432/agent_platform"
    persistence_mode: str = "memory"
    runtime_driver: str = "memory"
    runtime_context: str = "desktop-linux"
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

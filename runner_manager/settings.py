"""Runtime configuration for the host-side runner manager."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


@dataclass
class ManagerSettings:
    """Everything the manager needs; all values come from the host environment."""

    #: Control plane base URL (host-visible, e.g. http://127.0.0.1:8000).
    platform_url: str = field(default_factory=lambda: _env("AGENTSUPPORT_PLATFORM_URL", "http://127.0.0.1:8000"))
    #: Shared internal token (same value the platform uses for /internal/*).
    token: str = field(default_factory=lambda: _env("AGENTSUPPORT_RUNNER_MANAGER_TOKEN", ""))
    #: Docker CLI transport: local | wsl | context.
    docker_transport: str = field(default_factory=lambda: _env("AGENTSUPPORT_MANAGER_DOCKER_TRANSPORT", "auto"))
    docker_context: str = field(default_factory=lambda: _env("AGENTSUPPORT_MANAGER_DOCKER_CONTEXT", ""))
    wsl_distribution: str = field(default_factory=lambda: _env("AGENTSUPPORT_MANAGER_WSL_DISTRO", "Ubuntu"))
    #: Base image every tenant image is built FROM.
    base_image: str = field(default_factory=lambda: _env("AGENTSUPPORT_MANAGER_BASE_IMAGE", "agentsupport-api"))
    #: Host the control plane uses to reach a runner's published port.
    endpoint_host: str = field(default_factory=lambda: _env("AGENTSUPPORT_MANAGER_ENDPOINT_HOST", "http://host.docker.internal"))
    #: Host directory mounted into runners as the shared workspace root.
    workspace_root: str = field(default_factory=lambda: _env("AGENTSUPPORT_MANAGER_WORKSPACE_ROOT", ""))
    staging_root: Path = field(
        default_factory=lambda: Path(
            _env(
                "AGENTSUPPORT_MANAGER_STAGING_ROOT",
                str(Path(tempfile.gettempdir()) / "agentsupport-builds"),
            )
        )
    )
    #: Seconds an idle runner container stays up before it is reclaimed.
    idle_seconds: float = field(
        default_factory=lambda: float(_env("AGENTSUPPORT_MANAGER_IDLE_SECONDS", "300"))
    )
    #: Seconds to wait for a freshly launched runner to answer /ready.
    ready_timeout_seconds: float = field(
        default_factory=lambda: float(_env("AGENTSUPPORT_MANAGER_READY_TIMEOUT_SECONDS", "120"))
    )
    #: Seconds between build-queue polls when nothing is pending.
    poll_interval_seconds: float = field(
        default_factory=lambda: float(_env("AGENTSUPPORT_MANAGER_POLL_SECONDS", "5"))
    )
    #: Extra key=value environment entries handed to every runner container.
    runner_env: dict[str, str] = field(default_factory=dict)

    def resolved_transport(self) -> str:
        if self.docker_transport != "auto":
            return self.docker_transport
        if self.docker_context:
            return "context"
        return "wsl" if os.name == "nt" else "local"

    def passthrough_runner_env(self) -> dict[str, str]:
        """Runner env inherited from the host (model credentials, tokens…)."""

        prefixes = ("TRAE_", "SESSION_RUNNER_", "AGENTSUPPORT_")
        inherited = {
            key: value
            for key, value in os.environ.items()
            if key.startswith(prefixes)
        }
        inherited.update(self.runner_env)
        return inherited


__all__ = ["ManagerSettings"]

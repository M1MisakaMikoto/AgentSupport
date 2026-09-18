"""Host-side runner manager: tenant image builds and on-demand runners.

The manager runs *outside* the Compose stack (on the Docker host), because it
needs Docker access: it claims PENDING preset builds from the control plane,
builds one runner image per tenant preset, launches session runners on demand
from the matching image, and reclaims idle ones.
"""

from .manager import CommandResult, RunnerManager, SubprocessCommandRunner
from .settings import ManagerSettings

__all__ = [
    "CommandResult",
    "ManagerSettings",
    "RunnerManager",
    "SubprocessCommandRunner",
]

"""Convenience facade for the AgentSupport application service."""

from .application.service import AgentSupportService as ApplicationAgentSupportService
from .application.service import ServiceError
from .bootstrap.container import build_agentsupport_dependencies
from .bootstrap.settings import Settings, settings


class AgentSupportService(ApplicationAgentSupportService):
    def __init__(self, config: Settings = settings) -> None:
        super().__init__(config, **build_agentsupport_dependencies(config))


__all__ = ["AgentSupportService", "ServiceError"]

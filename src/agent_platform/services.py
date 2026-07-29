"""Compatibility facade for the platform application service."""

from .application.platform_service import PlatformService as ApplicationPlatformService
from .application.platform_service import ServiceError
from .bootstrap.container import build_platform_dependencies
from .bootstrap.settings import Settings, settings


class PlatformService(ApplicationPlatformService):
    def __init__(self, config: Settings = settings) -> None:
        super().__init__(config, **build_platform_dependencies(config))

__all__ = ["PlatformService", "ServiceError"]

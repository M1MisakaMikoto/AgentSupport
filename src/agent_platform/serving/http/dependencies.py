from fastapi import Request

from ...application.platform_service import PlatformService


def platform_service(request: Request) -> PlatformService:
    return request.app.state.service

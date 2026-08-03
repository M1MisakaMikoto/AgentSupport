from fastapi import Request

from ...application.service import AgentSupportService


def agentsupport_service(request: Request) -> AgentSupportService:
    return request.app.state.service

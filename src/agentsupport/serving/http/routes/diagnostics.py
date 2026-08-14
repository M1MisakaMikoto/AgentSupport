"""Control-plane diagnostics routed to the private Session Runner."""

import httpx
from fastapi import APIRouter, Request

from ....application.service import ServiceError
from ..dependencies import agentsupport_service

router = APIRouter()


@router.get("/diagnostics/model-connectivity")
async def model_connectivity(request: Request):
    service = agentsupport_service(request)
    if service.core_runtime is None:
        raise ServiceError(
            "DIAGNOSTICS_UNAVAILABLE",
            "runner diagnostics require a configured core runner (distributed mode)",
            503,
        )
    try:
        return await service.core_runtime.model_connectivity()
    except httpx.HTTPError as exc:
        raise ServiceError(
            "RUNNER_UNAVAILABLE",
            f"session runner is unreachable: {exc}",
            502,
        ) from exc

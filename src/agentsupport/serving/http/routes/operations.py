from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from ..dependencies import agentsupport_service

router = APIRouter()


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> dict[str, Any]:
    return agentsupport_service(request).readiness()


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(request: Request) -> str:
    values = agentsupport_service(request).metrics()
    return "".join(f"agentsupport_{name} {value}\n" for name, value in sorted(values.items()))


@router.get("/cores")
async def list_cores(request: Request):
    return agentsupport_service(request).runner_snapshot()

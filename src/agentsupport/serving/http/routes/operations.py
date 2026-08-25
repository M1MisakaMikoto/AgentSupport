from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from ....observability import metrics as obs_metrics
from ..dependencies import agentsupport_service

router = APIRouter()


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
def ready(request: Request) -> dict[str, Any]:
    return agentsupport_service(request).readiness()


@router.get("/metrics", response_class=PlainTextResponse)
def metrics(request: Request) -> str:
    service = agentsupport_service(request)
    obs_metrics.publish_coordination(service.metrics())
    obs_metrics.set_runners_registered(len(service.runner_snapshot()))
    body, _content_type = obs_metrics.render_metrics()
    return body.decode("utf-8")


@router.get("/cores")
def list_cores(request: Request):
    return agentsupport_service(request).runner_snapshot()

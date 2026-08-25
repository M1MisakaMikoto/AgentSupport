"""Internal Runner self-registration endpoints (token-gated, not public API)."""

from uuid import UUID

from fastapi import APIRouter, Request

from agent_runner_contracts.registration import RunnerHeartbeat, RunnerRegistrationRequest

from ..dependencies import agentsupport_service

router = APIRouter()


@router.post("/runners/register", status_code=201, include_in_schema=False)
def register_runner(
    request: Request, payload: RunnerRegistrationRequest
) -> dict:
    return agentsupport_service(request).register_runner(
        payload, bootstrap_token=request.headers.get("X-Runner-Token")
    ).model_dump(mode="json")


@router.post("/runners/{runner_id}/heartbeat", include_in_schema=False)
def runner_heartbeat(request: Request, runner_id: UUID, payload: RunnerHeartbeat) -> dict:
    return agentsupport_service(request).runner_heartbeat(
        runner_id, payload, runner_token=request.headers.get("X-Runner-Token")
    ).model_dump(mode="json")


@router.delete("/runners/{runner_id}", status_code=204, include_in_schema=False)
def deregister_runner(request: Request, runner_id: UUID) -> None:
    agentsupport_service(request).runner_deregister(
        runner_id, runner_token=request.headers.get("X-Runner-Token")
    )

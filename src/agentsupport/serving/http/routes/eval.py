"""Trial evaluation API: datasets, cases, runs, reports and comparisons.

Phase-1 trial surface.  Kept outside the official API reference until the
landing experiment validates the closed loop.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, Field

from ....evaluation import EvalService, VerifierConfig

router = APIRouter(prefix="/eval", tags=["evaluation"])


def eval_service(request: Request) -> EvalService:
    return request.app.state.eval_service


class EvalDatasetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    workspace_id: UUID
    baseline_version: str
    labels: dict[str, str] = Field(default_factory=dict)


class EvalCaseCreate(BaseModel):
    task: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    verifiers: list[VerifierConfig] = Field(default_factory=list)


class EvalRunCreate(BaseModel):
    dataset_id: UUID
    runner_fingerprint: dict[str, str] = Field(default_factory=dict)


@router.post("/datasets", status_code=201)
async def create_dataset(
    request: Request,
    body: EvalDatasetCreate,
    idempotency_key: str | None = Header(default=None),
):
    dataset = eval_service(request).create_dataset(
        name=body.name,
        workspace_id=body.workspace_id,
        baseline_version=body.baseline_version,
        description=body.description,
        labels=body.labels,
        idempotency_key=idempotency_key,
    )
    return dataset.model_dump(mode="json")


@router.get("/datasets")
async def list_datasets(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    return [
        item.model_dump(mode="json")
        for item in eval_service(request).list_datasets(limit=limit, offset=offset)
    ]


@router.get("/datasets/{dataset_id}")
async def get_dataset(request: Request, dataset_id: UUID):
    return eval_service(request).get_dataset(dataset_id).model_dump(mode="json")


@router.post("/datasets/{dataset_id}/cases", status_code=201)
async def add_case(request: Request, dataset_id: UUID, body: EvalCaseCreate):
    case = eval_service(request).add_case(
        dataset_id,
        task=body.task,
        tags=body.tags,
        verifiers=body.verifiers,
    )
    return case.model_dump(mode="json")


@router.post("/runs", status_code=202)
async def create_run(
    request: Request,
    body: EvalRunCreate,
    idempotency_key: str | None = Header(default=None),
):
    run = eval_service(request).start_run(
        body.dataset_id,
        runner_fingerprint=body.runner_fingerprint,
        idempotency_key=idempotency_key,
    )
    return run.model_dump(mode="json")


@router.get("/runs")
async def list_runs(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    return [
        item.model_dump(mode="json")
        for item in eval_service(request).list_runs(limit=limit, offset=offset)
    ]


@router.get("/runs/{run_id}")
async def get_run(request: Request, run_id: UUID):
    return eval_service(request).get_run(run_id).model_dump(mode="json")


@router.get("/runs/{run_id}/report")
async def get_report(request: Request, run_id: UUID):
    return eval_service(request).report(run_id).model_dump(mode="json")


@router.get("/runs/{run_id}/compare")
async def compare_runs(request: Request, run_id: UUID, baseline: UUID):
    return eval_service(request).compare(baseline, run_id)

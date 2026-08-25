"""Eval run failure semantics: per-case errors vs dataset-level failures."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from agentsupport.evaluation.domain import EvalCaseResult
from agentsupport.evaluation.service import EvalService
from agentsupport.evaluation.store import InMemoryEvalStore


class FakeWorkspaceDriver:
    def create_from_version(self, workspace_id: UUID, version_id: str) -> tuple[UUID, str]:
        return uuid4(), "/tmp/eval-ws"

    def list_versions(self, workspace_id: UUID) -> list[dict[str, object]]:
        return [{"version_id": "v1"}]


class FakeAgentSupport:
    """Minimal stand-in; ``_run_case`` is replaced in these tests."""


def _service(store: InMemoryEvalStore | None = None) -> EvalService:
    return EvalService(
        workspace_driver=FakeWorkspaceDriver(),
        agentsupport=FakeAgentSupport(),  # type: ignore[arg-type]
        store=store or InMemoryEvalStore(),
    )


def _dataset_with_cases(service: EvalService, tasks: list[str]):
    dataset = service.create_dataset(
        name="failure-dataset",
        workspace_id=uuid4(),
        baseline_version="v1",
    )
    for task in tasks:
        service.add_case(dataset.id, task=task)
    return dataset


@pytest.mark.asyncio
async def test_per_case_failure_is_error_and_remaining_cases_run():
    service = _service()
    dataset = _dataset_with_cases(service, ["good", "bad", "good2"])

    async def fake_run_case(case, dataset):
        if case.task == "bad":
            raise RuntimeError("runner exploded")
        return EvalCaseResult(case_id=case.id, task=case.task, verdict="PASS", score=1.0)

    service._run_case = fake_run_case  # type: ignore[method-assign]
    run = await service.run_dataset(dataset.id)

    assert run.status == "completed"
    assert run.completed_at is not None
    assert len(run.results) == 3
    by_task = {result.task: result for result in run.results}
    assert by_task["good"].verdict == "PASS"
    assert by_task["good2"].verdict == "PASS"
    assert by_task["bad"].verdict == "ERROR"
    assert by_task["bad"].verifier_results[0].reason == "runner exploded"
    assert by_task["bad"].verifier_results[0].evidence == ["RuntimeError"]
    assert run.summary["total"] == 3
    assert run.summary["passed"] == 2
    assert run.summary["error"] == 1


@pytest.mark.asyncio
async def test_persistence_failure_marks_run_failed_and_raises():
    class ExplodingStore(InMemoryEvalStore):
        def __init__(self) -> None:
            super().__init__()
            self.saves = 0
            self.last_run = None

        def save_run(self, run) -> None:
            self.saves += 1
            self.last_run = run
            if self.saves >= 3:
                raise RuntimeError("db unavailable")
            super().save_run(run)

    store = ExplodingStore()
    service = _service(store)
    dataset = _dataset_with_cases(service, ["task"])

    async def fake_run_case(case, dataset):
        return EvalCaseResult(case_id=case.id, task=case.task, verdict="PASS", score=1.0)

    service._run_case = fake_run_case  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="db unavailable"):
        await service.run_dataset(dataset.id)

    assert store.last_run is not None
    assert store.last_run.status == "failed"
    assert store.last_run.summary["error"]["code"] == "RuntimeError"
    assert store.last_run.summary["error"]["message"] == "db unavailable"

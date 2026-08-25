"""Temporal-mode eval cases: wait for the workflow, then evaluate."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from agentsupport.domain import Conversation, ExecutionState
from agentsupport.evaluation.service import EvalService
from agentsupport.evaluation.store import InMemoryEvalStore


class FakeTemporal:
    def __init__(self) -> None:
        self.wait_calls: list[tuple[str, float | None]] = []
        self.cancel_calls: list[str] = []

    async def wait_for_run(self, run_id: str, timeout_seconds: float | None = None) -> dict:
        self.wait_calls.append((run_id, timeout_seconds))
        return {"status": "terminal", "state": "completed"}

    async def cancel(self, run_id: str) -> None:
        self.cancel_calls.append(run_id)


class TimeoutTemporal(FakeTemporal):
    async def wait_for_run(self, run_id: str, timeout_seconds: float | None = None) -> dict:
        await super().wait_for_run(run_id, timeout_seconds)
        raise TimeoutError(f"run {run_id} timed out")


class FailedNonTerminalTemporal(FakeTemporal):
    async def wait_for_run(self, run_id: str, timeout_seconds: float | None = None) -> dict:
        await super().wait_for_run(run_id, timeout_seconds)
        return {"status": "failed", "error": "workflow terminated"}


class FakeWorkspaceDriver:
    def create_from_version(self, workspace_id: UUID, version_id: str) -> tuple[UUID, str]:
        return uuid4(), "/tmp/eval-ws"

    def list_versions(self, workspace_id: UUID) -> list[dict[str, object]]:
        return [{"version_id": "v1"}]


class FakeAgentSupport:
    temporal_mode = True

    def __init__(self, temporal: FakeTemporal, conversation: Conversation) -> None:
        self.temporal = temporal
        self._conversation = conversation

    def create_session(self, workspace_id: UUID, idempotency_key: str | None = None, **kwargs):
        return SimpleNamespace(id=uuid4())

    async def create_conversation(self, session_id: UUID, task: str) -> Conversation:
        return self._conversation

    def get_conversation(self, conversation_id: UUID) -> Conversation:
        return self._conversation

    def events(self, conversation_id: UUID, after_seq: int = 0) -> list:
        return []


def _completed_conversation() -> Conversation:
    conversation = Conversation(session_id=uuid4(), task="task")
    conversation.run.state = ExecutionState.COMPLETED
    conversation.run.result_summary = {"output": "ok"}
    return conversation


def _service(
    temporal: FakeTemporal, conversation: Conversation, case_timeout_seconds: float | None
) -> EvalService:
    return EvalService(
        workspace_driver=FakeWorkspaceDriver(),
        agentsupport=FakeAgentSupport(temporal, conversation),  # type: ignore[arg-type]
        store=InMemoryEvalStore(),
        case_timeout_seconds=case_timeout_seconds,
    )


@pytest.mark.asyncio
async def test_temporal_case_waits_for_workflow_before_outcome():
    conversation = _completed_conversation()
    temporal = FakeTemporal()
    service = _service(temporal, conversation, case_timeout_seconds=42.0)
    dataset = service.create_dataset(name="temporal-dataset", workspace_id=uuid4(), baseline_version="v1")
    service.add_case(dataset.id, task="task")

    run = await service.run_dataset(dataset.id)

    assert temporal.wait_calls == [(str(conversation.run.run_id), 42.0)]
    assert temporal.cancel_calls == []
    result = run.results[0]
    assert result.outcome is not None
    assert result.outcome.terminal_state == "COMPLETED"
    assert result.outcome.final_result == {"output": "ok"}


@pytest.mark.asyncio
async def test_temporal_timeout_cancels_workflow_and_marks_case_error():
    conversation = _completed_conversation()
    temporal = TimeoutTemporal()
    service = _service(temporal, conversation, case_timeout_seconds=5.0)
    dataset = service.create_dataset(name="timeout-dataset", workspace_id=uuid4(), baseline_version="v1")
    service.add_case(dataset.id, task="task")

    run = await service.run_dataset(dataset.id)

    assert run.status == "completed"  # per-case failure, not a dataset-level failure
    result = run.results[0]
    assert result.verdict == "ERROR"
    assert "timed out" in result.verifier_results[0].reason
    assert temporal.cancel_calls == [str(conversation.run.run_id)]


@pytest.mark.asyncio
async def test_non_terminal_workflow_end_is_case_error_not_stuck():
    conversation = _completed_conversation()
    conversation.run.state = ExecutionState.RUNNING  # repository state never became terminal
    temporal = FailedNonTerminalTemporal()
    service = _service(temporal, conversation, case_timeout_seconds=5.0)
    dataset = service.create_dataset(name="hard-fail-dataset", workspace_id=uuid4(), baseline_version="v1")
    service.add_case(dataset.id, task="task")

    run = await service.run_dataset(dataset.id)

    assert run.status == "completed"  # per-case failure, not a dataset-level failure
    result = run.results[0]
    assert result.verdict == "ERROR"
    assert "terminal conversation state" in result.verifier_results[0].reason
    assert "workflow terminated" in result.verifier_results[0].reason
    assert temporal.cancel_calls == []


@pytest.mark.asyncio
async def test_terminal_failed_conversation_is_evaluated_normally():
    conversation = _completed_conversation()
    conversation.run.state = ExecutionState.FAILED
    conversation.run.error = {"code": "WORKFLOW_FAILED", "message": "boom"}
    temporal = FakeTemporal()
    service = _service(temporal, conversation, case_timeout_seconds=5.0)
    dataset = service.create_dataset(name="failed-dataset", workspace_id=uuid4(), baseline_version="v1")
    service.add_case(dataset.id, task="task")

    run = await service.run_dataset(dataset.id)

    assert run.status == "completed"
    result = run.results[0]
    assert result.outcome is not None
    assert result.outcome.terminal_state == "FAILED"
    assert result.outcome.error == {"code": "WORKFLOW_FAILED", "message": "boom"}

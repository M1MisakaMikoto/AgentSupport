"""Temporal consistency fallbacks without a dev server.

The full workflow integration stays environment-gated (``tests/integration/
temporal`` needs a reachable server).  These tests drive the activities
directly against a SQLite repository so the heartbeat lifecycle and the
``fail_run`` terminal-state fallback are verified in the regular suite.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest

from agent_runner_contracts.events import EventEnvelope
from agentsupport.adapters.persistence.sqlalchemy.repository import PostgresRepository
from agentsupport.adapters.skills.local import LocalSkillProvider
from agentsupport.bootstrap.settings import Settings
from agentsupport.domain import Conversation, ExecutionState
from agentsupport.execution.temporal import activities as temporal_activities
from agentsupport.execution.temporal.activities import (
    ExecutionContext,
    _heartbeat_until,
    execute_run,
    fail_run,
    set_execution_context,
)


class CompletedRuntime:
    """Fake CoreRuntime that immediately completes a run segment."""

    async def run(self, request: dict, event_sink) -> dict:
        # Give the heartbeat loop a chance to run, as real segments do.
        await asyncio.sleep(0.05)
        run_id = UUID(request["run_id"])
        await event_sink(
            EventEnvelope(
                run_id=run_id,
                seq=1,
                type="run.completed",
                payload={"result": {"status": "completed"}},
                source="runner",
            )
        )
        return {"status": "completed"}


@pytest.fixture
def env(tmp_path):
    config = Settings(
        persistence_mode="postgres",
        database_url=f"sqlite:///{tmp_path / 'activities.db'}",
        auto_create_schema=True,
        workspace_root=tmp_path / "workspace",
        skills_root=tmp_path / "skills",
    )
    repository = PostgresRepository(config.database_url, create_schema=True)
    skills_root = tmp_path / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)

    workspace = repository.create_workspace(
        "ws", str(tmp_path / "workspace" / "ws"), "hash-ws", None
    )
    session = repository.create_session(workspace, "hash-session", None)
    conversation = Conversation(session_id=session.id, task="unit test")
    conversation.run.state = ExecutionState.RUNNING
    conversation = repository.create_conversation(conversation, "hash-conversation", None)

    context = ExecutionContext(
        config=config,
        repository=repository,
        core_runtime=CompletedRuntime(),
    )
    context.skill_provider = LocalSkillProvider(skills_root)
    set_execution_context(context)
    return SimpleNamespace(
        config=config,
        repository=repository,
        workspace=workspace,
        session=session,
        conversation=conversation,
    )


def _request(env) -> dict:
    return {
        "run_id": str(env.conversation.run.run_id),
        "conversation_id": str(env.conversation.id),
        "session_id": str(env.session.id),
        "workspace_id": str(env.workspace.id),
    }


def test_heartbeat_loop_heartbeats_and_stops(monkeypatch):
    calls = []

    def fake_heartbeat(details) -> None:
        calls.append(details)

    monkeypatch.setattr(temporal_activities.activity, "heartbeat", fake_heartbeat)
    stop = asyncio.Event()

    async def scenario() -> None:
        task = asyncio.create_task(
            _heartbeat_until(stop, {"run_id": "run-1", "phase": "test"})
        )
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())
    assert calls, "heartbeat must fire before the stop signal"
    assert calls[0] == {"run_id": "run-1", "phase": "test"}


@pytest.mark.asyncio
async def test_execute_run_runs_heartbeat_and_completes(env, monkeypatch):
    calls = []
    monkeypatch.setattr(
        temporal_activities.activity,
        "heartbeat",
        lambda details: calls.append(details),
    )
    result = await execute_run(_request(env))

    assert result["status"] == "terminal"
    assert result["state"] == ExecutionState.COMPLETED.value
    assert calls, "execute_run must heartbeat during the segment"
    events = env.repository.list_events(env.conversation.id)
    assert events[-1].type == "run.completed"


@pytest.mark.asyncio
async def test_fail_run_marks_conversation_failed(env):
    result = await fail_run(
        {
            "request": _request(env),
            "error": {"code": "WORKFLOW_FAILED", "message": "retries exhausted"},
        }
    )
    assert result["status"] == "failed"

    conversation = env.repository.get_conversation(env.conversation.id)
    assert conversation is not None
    assert conversation.run.state == ExecutionState.FAILED
    assert conversation.run.error is not None
    assert conversation.run.error["code"] == "WORKFLOW_FAILED"

    events = env.repository.list_events(env.conversation.id)
    assert events[-1].type == "run.failed"
    assert events[-1].payload["code"] == "WORKFLOW_FAILED"


@pytest.mark.asyncio
async def test_fail_run_is_idempotent_for_terminal_runs(env):
    first = await fail_run({"request": _request(env), "error": {}})
    assert first["status"] == "failed"
    count_after_first = len(env.repository.list_events(env.conversation.id))

    second = await fail_run(
        {"request": _request(env), "error": {"code": "WORKFLOW_FAILED", "message": "again"}}
    )
    assert second["status"] == "already_terminal"
    assert len(env.repository.list_events(env.conversation.id)) == count_after_first

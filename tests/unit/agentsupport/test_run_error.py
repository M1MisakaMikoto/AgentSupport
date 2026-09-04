"""Run-error projection: structured failure info must survive event application."""

from __future__ import annotations

import pytest

from agent_runner_contracts.events import EventEnvelope
from agentsupport.domain.execution import ExecutionState
from _support import make_temporal_service as _make_service


@pytest.mark.asyncio
async def test_apply_core_event_captures_run_error(tmp_path):
    env = _make_service(tmp_path)
    service = env.service
    workspace = service.create_workspace("ws")
    session = service.create_session(workspace.id)
    conversation = await service.create_conversation(session.id, "hi")
    event = EventEnvelope(
        run_id=conversation.run.run_id,
        seq=conversation.run.last_seq + 1,
        type="run.failed",
        payload={"code": "TRAE_RUNTIME_ERROR", "message": "Connection error."},
        source="session_runner",
    )

    service._apply_core_event(conversation, event)

    assert conversation.run.state == ExecutionState.FAILED
    assert conversation.run.error == {
        "code": "TRAE_RUNTIME_ERROR",
        "message": "Connection error.",
    }
    assert service.events_store.list(conversation.id)[-1].type == "run.failed"
    assert env.repository.get_conversation(conversation.id) is not None


def test_run_projection_defaults_have_no_error():
    from agentsupport.domain import RunProjection

    assert RunProjection().error is None

"""Run-error projection: structured failure info must survive event application."""

from uuid import uuid4

from agent_runner_contracts.events import EventEnvelope
from agentsupport.config import Settings
from agentsupport.domain import Conversation
from agentsupport.domain.execution import ExecutionState
from agentsupport.services import AgentSupportService


def test_apply_core_event_captures_run_error(tmp_path):
    service = AgentSupportService(Settings(workspace_root=tmp_path))
    conversation = Conversation(session_id=uuid4(), task="hi")
    event = EventEnvelope(
        run_id=conversation.run.run_id,
        seq=1,
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


def test_run_projection_defaults_have_no_error():
    from agentsupport.domain import RunProjection

    assert RunProjection().error is None

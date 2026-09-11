"""Temporal run-request builder mirrors the inline context_bundle fields."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from agent_runner_contracts.events import EventEnvelope
from agentsupport.execution.temporal.activities import (
    ExecutionContext,
    _build_run_request,
)
from agentsupport.domain import ConversationMode


def _stub_context(
    *,
    conversations: list | None = None,
    events: list[EventEnvelope] | None = None,
) -> ExecutionContext:
    conversations = conversations or []
    events = events or []
    repository = SimpleNamespace(
        list_ready_runner_registrations=lambda: [],
        list_conversations=lambda session_id=None: conversations,
        list_session_events=lambda session_id: events,
    )
    skill_provider = SimpleNamespace(
        skill_catalog=lambda skills, tenant_id=None: [],
        skill_package=lambda skills, tenant_id=None: [],
    )
    return ExecutionContext(
        config=SimpleNamespace(
            core_runner_workspace_root=None,
            core_runner_url="http://runner:8080",
        ),
        repository=repository,
        core_runtime=SimpleNamespace(),
        skill_provider=skill_provider,
    )


def _build(
    file_ref_format: bool,
    *,
    conversations: list | None = None,
    events: list[EventEnvelope] | None = None,
) -> dict:
    session = SimpleNamespace(
        id=uuid4(),
        tenant_id="t-demo",
        lease_epoch=0,
        config=SimpleNamespace(file_ref_format=file_ref_format),
    )
    conversation = SimpleNamespace(
        id=uuid4(),
        task="create a file",
        run=SimpleNamespace(run_id=uuid4()),
    )
    workspace = SimpleNamespace(root_path="/tmp/workspace")
    request = {
        "run_id": str(uuid4()),
        "conversation_id": str(conversation.id),
        "session_id": str(session.id),
        "skills": [],
        "tool_policy": {},
        "mcp_refs": [],
    }
    return _build_run_request(
        request,
        conversation,
        session,
        workspace,
        _stub_context(conversations=conversations, events=events),
    )


def test_temporal_run_request_carries_file_ref_format_flag() -> None:
    bundle = _build(file_ref_format=True)["context_bundle"]
    assert bundle["file_ref_format"] is True


def test_temporal_run_request_defaults_file_ref_format_off() -> None:
    bundle = _build(file_ref_format=False)["context_bundle"]
    assert bundle["file_ref_format"] is False


def test_temporal_run_request_excludes_silent_generation_conversation() -> None:
    silent_run = uuid4()
    silent = SimpleNamespace(
        id=uuid4(),
        mode=ConversationMode.SILENT,
        run=SimpleNamespace(run_id=silent_run),
        task="你是 skill 提炼 agent…",
        created_at="2026-09-04T00:00:00Z",
    )
    event = EventEnvelope(
        run_id=silent_run,
        seq=1,
        type="message",
        payload={"content": "生成内容"},
        source="runner",
    )
    request = _build(
        file_ref_format=False,
        conversations=[silent],
        events=[event],
    )
    recent = request["context_bundle"]["recent_events"]
    assert str(silent_run) not in [str(e.get("run_id")) for e in recent]
    assert not any("skill 提炼" in str(e.get("payload")) for e in recent)

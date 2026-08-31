"""Conversation-level execution modes flow into the run request tool policy."""

from __future__ import annotations

from uuid import UUID

from agent_runner_contracts.events import EventEnvelope
from agentsupport.config import Settings
from agentsupport.domain import ConversationMode
from agentsupport.services import AgentSupportService

SILENT_TOOLS = ["str_replace_based_edit_tool", "json_edit_tool", "task_done"]


class CapturingCore:
    def __init__(self):
        self.captured = []

    async def run(self, request, event_sink):
        self.captured.append(request)
        await event_sink(
            EventEnvelope(
                run_id=UUID(request["run_id"]),
                seq=1,
                type="run.completed",
                payload={"result": {"status": "completed", "content": "done"}},
                source="runner",
            )
        )
        return {"status": "COMPLETED", "events": []}

    def register_run_endpoint(self, run_id, endpoint):
        return None


class StubDriver:
    async def start(self, session_id, workspace_path, lease_epoch, workspace_id=None, read_only_mounts=None, runtime_operation_id=None):
        return "c"

    async def stop(self, container_id, *, force=False):
        return True

    async def inspect(self, container_id):
        return {"status": "running"}

    async def endpoint(self, container_id):
        return None


def _service(tmp_path):
    service = AgentSupportService(
        Settings(
            workspace_root=tmp_path / "workspaces",
            skills_root=tmp_path / "skills",
        )
    )
    service.runtime_driver = StubDriver()
    service.core_runtime = CapturingCore()
    return service


async def _session(service):
    workspace = service.create_workspace("ws")
    return service.create_session(workspace.id, tenant_id="t-1", name="s")


async def test_silent_mode_policy_in_run_request(tmp_path):
    service = _service(tmp_path)
    session = await _session(service)

    conversation = await service.create_conversation(
        session.id, "task", mode=ConversationMode.SILENT
    )

    assert conversation.mode == ConversationMode.SILENT
    policy = service.core_runtime.captured[0]["tool_policy"]
    assert policy["mode"] == "silent"
    assert policy["allowed_tools"] == SILENT_TOOLS
    assert policy["approval_required_tools"] == []


async def test_no_approval_mode_policy_in_run_request(tmp_path):
    service = _service(tmp_path)
    session = await _session(service)

    await service.create_conversation(session.id, "task", mode=ConversationMode.NO_APPROVAL)

    policy = service.core_runtime.captured[0]["tool_policy"]
    assert policy["mode"] == "no_approval"
    assert policy["approval_required_tools"] == []


async def test_default_mode_policy_unmarked(tmp_path):
    service = _service(tmp_path)
    session = await _session(service)

    await service.create_conversation(session.id, "task")

    policy = service.core_runtime.captured[0]["tool_policy"]
    assert "mode" not in policy

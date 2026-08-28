"""Session-wide context: later conversations see earlier dialogue history."""

from uuid import UUID

from agent_runner_contracts.events import EventEnvelope
from agentsupport.config import Settings
from agentsupport.services import AgentSupportService


class _CapturingCore:
    def __init__(self):
        self.captured: list[dict] = []

    async def run(self, request: dict, event_sink):
        self.captured.append(request["context_bundle"]["recent_events"])
        await event_sink(
            EventEnvelope(
                run_id=UUID(request["run_id"]),
                seq=1,
                type="run.completed",
                payload={"result": {"status": "completed", "content": "done"}},
                source="runner",
            )
        )
        return {"status": "RUNNING", "events": []}

    def register_run_endpoint(self, run_id, endpoint):
        return None


class _FakeRuntimeDriver:
    async def start(self, session_id, workspace_path, lease_epoch, workspace_id=None, read_only_mounts=None, runtime_operation_id=None):
        return f"container-{session_id}"

    async def stop(self, container_id, *, force=False):
        return True

    async def inspect(self, container_id):
        return {"status": "running"}

    async def endpoint(self, container_id):
        return None


def _service(tmp_path):
    svc = AgentSupportService(
        Settings(
            workspace_root=tmp_path / "workspaces",
            skills_root=tmp_path / "skills",
        )
    )
    svc.runtime_driver = _FakeRuntimeDriver()
    return svc


async def test_second_conversation_sees_first_round_dialogue(tmp_path):
    core = _CapturingCore()
    service = _service(tmp_path)
    service.core_runtime = core

    workspace = service.create_workspace("ctx")
    session = service.create_session(workspace.id, tenant_id="t-1")
    first = await service.create_conversation(session.id, "记住规则：收到 /close 就回复 /close")
    await service.create_conversation(session.id, "/close", parent_conversation_id=first.id)

    assert len(core.captured) == 2
    first_events = core.captured[0]
    second_events = core.captured[1]
    # second round must include the first round's instruction message
    first_task_events = [
        e for e in second_events
        if e["type"] == "message" and "记住规则" in str(e.get("payload", {}).get("content", ""))
    ]
    assert first_task_events, "second conversation context is missing the earlier instruction"
    # context is session-wide: it also carries the first conversation's completion
    assert any(e["type"] == "run.completed" for e in second_events)
    # per-conversation tool history is still present (no cross-contamination beyond limit)
    assert len(second_events) >= len(first_events)
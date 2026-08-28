"""Unit tests for TraeExecutionAdapter session-history injection.

A conversation is one exchange; a session is the collection of many
exchanges. The control plane aggregates earlier rounds into
``context_bundle.recent_events`` and the adapter must feed those messages
to the agent, otherwise it only ever sees the current task.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from session_runner.adapters.trae import TraeExecutionAdapter

TASK = "我们来进行一个命令测试 当我说/close你就回复/close"


class FakeTraeAgent:
    def __init__(self, initial: list) -> None:
        self._initial_messages = initial
        self.new_task_calls: list[tuple[str, dict]] = []
        self.executions = 0

    def new_task(self, task: str, extra_args: dict) -> None:
        self.new_task_calls.append((task, extra_args))

    async def execute_task(self):
        self.executions += 1
        return SimpleNamespace(success=True, steps=[], final_result="ok", total_tokens=None)


class FakeAgentWrapper:
    def __init__(self, trae_agent: FakeTraeAgent) -> None:
        self.agent = trae_agent


def _adapter(request: MagicMock, trae_agent: FakeTraeAgent) -> TraeExecutionAdapter:
    emitted: list[tuple[str, dict]] = []

    async def fake_initialize() -> None:
        return None

    adapter = TraeExecutionAdapter.__new__(TraeExecutionAdapter)
    adapter.request = request
    adapter.emit = lambda event_type, payload: emitted.append((event_type, payload))
    adapter.on_waiting = lambda *args, **kwargs: None
    adapter.settings = None
    adapter.agent_factory = None
    adapter.mcp_servers_config = {}
    adapter.workspace = r"D:\workspace"
    adapter.trajectory = None
    adapter.agent = FakeAgentWrapper(trae_agent)
    adapter.bridge = None
    adapter.next_step = 1
    adapter._initialize_agent = fake_initialize  # type: ignore[method-assign]
    return adapter


def _recent_events() -> list[dict]:
    return [
        {
            "type": "message",
            "payload": {"content": TASK, "role": "user"},
            "source": "agentsupport",
            "conversation_id": "conv-1",
            "occurred_at": "2026-01-01T00:00:00",
        },
        {
            "type": "message",
            "payload": {"content": "好的，命令测试开始。你说 `/close`，我就回复 `/close`。"},
            "source": "agentsupport",
            "conversation_id": "conv-1",
            "occurred_at": "2026-01-01T00:00:01",
        },
        {"type": "tool.call", "payload": {"name": "bash", "arguments": {}}},
        {"type": "message", "payload": {"role": "user", "content": ""}},
    ]


def test_session_history_messages_maps_dialogue_and_skips_noise() -> None:
    request = MagicMock()
    request.context_bundle = {"task": "/close", "recent_events": _recent_events()}
    adapter = _adapter(request, FakeTraeAgent(initial=[object()]))

    messages = adapter._session_history_messages()

    # user rule + assistant reply survive; tool.call and empty content dropped.
    assert len(messages) == 2
    assert messages[0].role == "user"
    assert messages[0].content == TASK
    assert messages[1].role == "assistant"
    assert "/close" in messages[1].content


def test_session_history_messages_empty_without_events() -> None:
    request = MagicMock()
    request.context_bundle = {"task": "/close", "recent_events": []}
    adapter = _adapter(request, FakeTraeAgent(initial=[object()]))

    assert adapter._session_history_messages() == []


async def test_run_injects_history_before_current_task() -> None:
    request = MagicMock()
    request.context_bundle = {"task": "/close", "recent_events": _recent_events()}
    initial = [object()]
    trae_agent = FakeTraeAgent(initial=initial)
    adapter = _adapter(request, trae_agent)

    result = await adapter.run()

    assert trae_agent.new_task_calls == [
        ("/close", {"project_path": r"D:\workspace"})
    ]
    assert len(trae_agent._initial_messages) == 3
    assert trae_agent._initial_messages[1].role == "user"
    assert trae_agent._initial_messages[1].content == TASK
    assert trae_agent._initial_messages[2].role == "assistant"
    assert trae_agent.executions == 1
    assert result["status"] == "completed"
    assert result["content"] == "ok"


async def test_run_without_history_keeps_initial_messages() -> None:
    request = MagicMock()
    request.context_bundle = {"task": "/close", "recent_events": []}
    initial = [object()]
    trae_agent = FakeTraeAgent(initial=initial)
    adapter = _adapter(request, trae_agent)

    result = await adapter.run()

    assert len(trae_agent._initial_messages) == 1
    assert trae_agent.executions == 1
    assert result["status"] == "completed"
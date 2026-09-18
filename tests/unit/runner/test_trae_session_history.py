"""Unit tests for TraeExecutionAdapter session-history injection.

A conversation is one exchange; a session is the collection of many
exchanges. The control plane aggregates earlier rounds into
``context_bundle.recent_events`` and the adapter must feed those messages
to the agent, otherwise it only ever sees the current task.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from session_runner.adapters.trae import TraeExecutionAdapter

TASK = "我们来进行一个命令测试 当我说/close你就回复/close"


class FakeTraeAgent:
    def __init__(self, initial: list) -> None:
        self._initial_messages = initial
        self.final_result = "ok"
        self.new_task_calls: list[tuple[str, dict]] = []
        self.executions = 0

    def new_task(self, task: str, extra_args: dict) -> None:
        self.new_task_calls.append((task, extra_args))

    async def execute_task(self):
        self.executions += 1
        return SimpleNamespace(
            success=True, steps=[], final_result=self.final_result, total_tokens=None
        )


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


def test_failed_turn_is_part_of_the_context() -> None:
    """失败的那一轮也要进上下文：模型必须知道"上一轮失败了、文档可能只改了一半"。

    失败时那一轮没有 `message` 事件（run 死在半路），只认 message 的话上下文里就是
    两条相邻的 user 任务 —— 模型会以为上一轮没发生过，重头再来一遍。
    """
    events = [
        {"type": "message", "payload": {"role": "user", "content": "把标题改成企业知识库"}},
        {"type": "tool.call", "payload": {"name": "write_at"}},
        {"type": "run.failed", "payload": {"code": "WORKFLOW_FAILED",
                                           "message": "Activity task failed"}},
    ]
    request = MagicMock()
    request.context_bundle = {"task": "继续", "recent_events": events}
    adapter = _adapter(request, FakeTraeAgent(initial=[object()]))

    messages = adapter._session_history_messages()

    assert [m.role for m in messages] == ["user", "assistant"]
    assert "上一轮运行失败" in messages[1].content
    assert "Activity task failed" in messages[1].content
    assert "文档可能只改了一部分" in messages[1].content


def test_cancelled_turn_is_part_of_the_context() -> None:
    """用户主动停止的那一轮同理（否则模型不知道文档是"改到一半"的状态）。"""
    events = [
        {"type": "message", "payload": {"role": "user", "content": "改标题"}},
        {"type": "run.cancelled", "payload": {"reason": "user_cancelled"}},
    ]
    request = MagicMock()
    request.context_bundle = {"task": "继续", "recent_events": events}
    adapter = _adapter(request, FakeTraeAgent(initial=[object()]))

    messages = adapter._session_history_messages()

    assert [m.role for m in messages] == ["user", "assistant"]
    assert "上一轮被用户停止" in messages[1].content


def test_fallback_recovers_last_nonempty_assistant_text(tmp_path) -> None:
    traj = tmp_path / "traj.json"
    traj.write_text(
        json.dumps({"llm_interactions": [
            {"response": {"content": "", "finish_reason": "tool_use"}},
            {"response": {"content": "---\nname: my-skill\ndescription: x\n---"}},
        ]}),
        encoding="utf-8",
    )
    request = MagicMock()
    request.context_bundle = {"task": "t", "recent_events": []}
    adapter = _adapter(request, FakeTraeAgent(initial=[object()]))
    adapter.trajectory = traj

    assert "name: my-skill" in adapter._fallback_final_content()


def test_run_uses_fallback_when_final_result_empty(tmp_path) -> None:
    traj = tmp_path / "traj.json"
    traj.write_text(
        json.dumps({"llm_interactions": [
            {"response": {"content": "---\nname: my-skill\ndescription: x\n---"}}
        ]}),
        encoding="utf-8",
    )
    request = MagicMock()
    request.context_bundle = {"task": "t", "recent_events": []}
    trae_agent = FakeTraeAgent(initial=[object()])
    trae_agent.final_result = ""
    adapter = _adapter(request, trae_agent)
    adapter.trajectory = traj
    emitted: list[tuple[str, dict]] = []
    adapter.emit = lambda event_type, payload: emitted.append((event_type, payload))

    result = asyncio.run(adapter.run())

    assert result["content"] == "---\nname: my-skill\ndescription: x\n---"
    assert result["status"] == "completed"
    warnings = [payload for event_type, payload in emitted if event_type == "run.warning"]
    assert warnings and warnings[0]["code"] == "CONTENT_DEGRADATION"
    assert warnings[0]["kind"] == "fallback"


def test_run_emits_warning_when_content_empty_and_no_fallback() -> None:
    request = MagicMock()
    request.context_bundle = {"task": "t", "recent_events": []}
    trae_agent = FakeTraeAgent(initial=[object()])
    trae_agent.final_result = ""
    adapter = _adapter(request, trae_agent)
    emitted: list[tuple[str, dict]] = []
    adapter.emit = lambda event_type, payload: emitted.append((event_type, payload))

    result = asyncio.run(adapter.run())

    assert result["content"] == ""
    assert result["status"] == "completed"
    warnings = [payload for event_type, payload in emitted if event_type == "run.warning"]
    assert warnings and warnings[0]["kind"] == "empty"

def test_run_keeps_original_final_result_when_present(tmp_path) -> None:
    traj = tmp_path / "traj.json"
    traj.write_text(
        json.dumps({"llm_interactions": [{"response": {"content": "stale text"}}]}),
        encoding="utf-8",
    )
    request = MagicMock()
    request.context_bundle = {"task": "t", "recent_events": []}
    trae_agent = FakeTraeAgent(initial=[object()])
    adapter = _adapter(request, trae_agent)
    adapter.trajectory = traj

    result = asyncio.run(adapter.run())

    assert result["content"] == "ok"


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

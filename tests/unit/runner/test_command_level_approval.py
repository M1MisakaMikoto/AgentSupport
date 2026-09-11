"""Default-mode approval sinks to the command level: safe reads need no human."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from session_runner.adapters.trae import TraeToolGatewayBridge
from session_runner.security import BashCallGate, CommandApproval, DenyAllApprover

TOOLS = ["bash", "str_replace_based_edit_tool", "task_done"]


class FakeDelegate:
    def __init__(self):
        self.executed: list = []

    async def sequential_tool_call(self, calls):
        self.executed.extend(calls)
        return [
            SimpleNamespace(call_id=c.call_id, name=c.name, success=True, result="ok", error=None)
            for c in calls
        ]

    async def parallel_tool_call(self, calls):
        return await self.sequential_tool_call(calls)


def _bash(call_id: str, command: str, reason: str = "读取 skill 说明"):
    return SimpleNamespace(
        call_id=call_id,
        id=None,
        name="bash",
        arguments={"command": command, "reason": reason},
    )


def _bridge(tmp_path, mode: str, approver=None):
    emitted: list[tuple[str, dict]] = []
    delegate = FakeDelegate()
    gate = BashCallGate(
        approver=approver or DenyAllApprover(),
        allowed_prefixes=(str(tmp_path),),
        cwd=str(tmp_path),
        mode=mode,
    )
    approvals = ["bash"] if mode == "default" else []
    bridge = TraeToolGatewayBridge(
        delegate,
        list(TOOLS),
        {"mode": mode, "allowed_tools": list(TOOLS), "approval_required_tools": approvals},
        lambda event_type, payload: emitted.append((event_type, payload)),
        lambda *args, **kwargs: None,
        lambda: 2,
        workspace_root=tmp_path,
        bash_gate=gate,
    )
    return bridge, delegate, emitted


def test_default_mode_whitelist_read_needs_no_human(tmp_path):
    (tmp_path / "report.txt").write_text("data", encoding="utf-8")
    bridge, delegate, emitted = _bridge(tmp_path, "default")
    call = _bash("c1", "cat report.txt")

    results = asyncio.run(bridge.sequential_tool_call([call]))

    assert results[0].success
    assert delegate.executed == [call]
    assert [t for t, _ in emitted if t == "interaction.requested"] == []
    gate_events = [p for t, p in emitted if t == "command.gate"]
    assert gate_events and gate_events[0]["tier"] == "safe"


def test_default_mode_risky_command_still_asks_the_human(tmp_path):
    bridge, _, _ = _bridge(tmp_path, "default")
    call = _bash("c1", "find . -name '*.md'")

    blocked, all_safe = bridge._preflight([call])
    policy = bridge._effective_policy(all_safe, restored_decision=None)
    from agent_runner_contracts.tools import AuthorizationStatus, ToolBatch, ToolCall

    batch = ToolBatch(
        calls=[ToolCall(call_id=call.call_id, name=call.name, arguments=call.arguments)]
    )

    assert blocked == {}
    assert all_safe is False
    assert bridge.control_plane.authorize(batch, policy).status is (
        AuthorizationStatus.REQUIRES_APPROVAL
    )


def test_blocked_batch_is_rejected_without_asking_the_human(tmp_path):
    bridge, delegate, emitted = _bridge(tmp_path, "default")
    call = _bash("c1", "rm -rf report.txt")

    results = asyncio.run(bridge.sequential_tool_call([call]))

    assert not results[0].success
    assert "不可用清单" in (results[0].error or "")
    assert delegate.executed == []
    assert [t for t, _ in emitted if t == "interaction.requested"] == []


def test_missing_reason_is_rejected_before_the_human_gate(tmp_path):
    bridge, delegate, emitted = _bridge(tmp_path, "default")
    call = _bash("c1", "cat report.txt", reason="   ")

    results = asyncio.run(bridge.sequential_tool_call([call]))

    assert not results[0].success
    assert "reason" in (results[0].error or "")
    assert delegate.executed == []
    assert [t for t, _ in emitted if t == "interaction.requested"] == []


def test_silent_mode_risky_read_is_decided_by_the_approver(tmp_path):
    class _Approver:
        async def approve(self, request):
            return CommandApproval(False, "看上去在读环境", "llm")

    bridge, delegate, emitted = _bridge(tmp_path, "silent", approver=_Approver())
    call = _bash("c1", "find . -name '*.md'")

    results = asyncio.run(bridge.sequential_tool_call([call]))

    assert not results[0].success
    assert "看上去在读环境" in (results[0].error or "")
    assert delegate.executed == []
    assert [t for t, _ in emitted if t == "interaction.requested"] == []

"""Runner tool-gateway mode tests: silent / no-approval / default."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from agent_runner_contracts.tools import (
    AuthorizationStatus,
    ToolBatch,
    ToolCall,
)
from session_runner.adapters.trae import TraeToolGatewayBridge

TOOLS = ["bash", "str_replace_based_edit_tool", "json_edit_tool", "task_done"]
WHITELIST = ["str_replace_based_edit_tool", "json_edit_tool", "task_done"]


class FakeDelegate:
    def __init__(self):
        self.executed: list[ToolCall] = []

    async def sequential_tool_call(self, calls):
        self.executed.extend(calls)
        return [
            SimpleNamespace(call_id=c.call_id, name=c.name, success=True, result="ok", error=None)
            for c in calls
        ]

    async def parallel_tool_call(self, calls):
        return await self.sequential_tool_call(calls)

    async def close_tools(self):
        return None


def _make_bridge(policy: dict, workspace_root):
    emitted: list[tuple[str, dict]] = []
    delegate = FakeDelegate()
    bridge = TraeToolGatewayBridge(
        delegate,
        list(TOOLS),
        policy,
        lambda event_type, payload: emitted.append((event_type, payload)),
        lambda *args, **kwargs: None,
        lambda: 2,
        workspace_root=workspace_root,
    )
    return bridge, delegate, emitted


def _call(name: str, path: str | None = None) -> ToolCall:
    arguments = {"command": "view"}
    if path is not None:
        arguments["path"] = path
    return ToolCall(call_id="c1", name=name, arguments=arguments)


def _authorize(bridge, call: ToolCall) -> str:
    return bridge.control_plane.authorize(ToolBatch(calls=[call]), bridge.policy).status


def test_silent_mode_allows_workspace_tool_without_approval(tmp_path):
    bridge, delegate, emitted = _make_bridge(
        {"mode": "silent", "allowed_tools": list(WHITELIST), "approval_required_tools": []},
        workspace_root=tmp_path,
    )
    call = _call("str_replace_based_edit_tool", str(tmp_path / "events.jsonl"))

    assert _authorize(bridge, call) == AuthorizationStatus.APPROVED
    results = asyncio.run(bridge.sequential_tool_call([call]))

    assert results[0].success
    assert len(delegate.executed) == 1
    assert [t for t, _ in emitted if t == "interaction.requested"] == []
    assert [t for t, _ in emitted if t == "run.warning"] == []


def test_silent_mode_rejects_outside_workspace_path(tmp_path):
    outside = tmp_path.parent / "outside.jsonl"
    bridge, delegate, emitted = _make_bridge(
        {"mode": "silent", "allowed_tools": list(WHITELIST), "approval_required_tools": []},
        workspace_root=tmp_path,
    )
    call = _call("str_replace_based_edit_tool", str(outside))

    results = asyncio.run(bridge.sequential_tool_call([call]))

    assert not results[0].success
    assert "outside workspace" in (results[0].error or "")
    assert delegate.executed == []
    warnings = [payload for t, payload in emitted if t == "run.warning"]
    assert warnings and warnings[0]["code"] == "SANDBOX_REJECTED"


def test_no_approval_mode_allows_bash_without_approval(tmp_path):
    bridge, delegate, _ = _make_bridge({"mode": "no_approval"}, workspace_root=tmp_path)
    call = _call("bash")

    assert _authorize(bridge, call) == AuthorizationStatus.APPROVED
    results = asyncio.run(bridge.sequential_tool_call([call]))

    assert results[0].success
    assert len(delegate.executed) == 1


def test_default_mode_keeps_side_effect_approval(tmp_path):
    bridge, _, _ = _make_bridge(
        {"allowed_tools": list(TOOLS), "approval_required_tools": []},
        workspace_root=tmp_path,
    )
    call = _call("str_replace_based_edit_tool", str(tmp_path / "events.jsonl"))

    assert _authorize(bridge, call) == AuthorizationStatus.REQUIRES_APPROVAL

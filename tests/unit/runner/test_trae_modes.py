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
DOC_TOOLS = ["word_edit_tool", "excel_edit_tool", "pdf_tool", "document_convert_tool"]


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


def _make_bridge(policy: dict, workspace_root, tool_names=None):
    emitted: list[tuple[str, dict]] = []
    delegate = FakeDelegate()
    bridge = TraeToolGatewayBridge(
        delegate,
        list(tool_names if tool_names is not None else TOOLS),
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


def test_silent_mode_sandboxes_document_tools(tmp_path):
    tool_names = list(TOOLS) + list(DOC_TOOLS)
    bridge, delegate, emitted = _make_bridge(
        {
            "mode": "silent",
            "allowed_tools": list(WHITELIST) + list(DOC_TOOLS),
            "approval_required_tools": [],
        },
        workspace_root=tmp_path,
        tool_names=tool_names,
    )
    call = _call("word_edit_tool", str(tmp_path.parent / "outside.docx"))

    results = asyncio.run(bridge.sequential_tool_call([call]))

    assert not results[0].success
    assert "outside workspace" in (results[0].error or "")
    assert delegate.executed == []
    warnings = [payload for t, payload in emitted if t == "run.warning"]
    assert warnings and warnings[0]["code"] == "SANDBOX_REJECTED"


def test_default_mode_requires_approval_for_document_tools(tmp_path):
    tool_names = list(TOOLS) + list(DOC_TOOLS)
    bridge, _, _ = _make_bridge(
        {"allowed_tools": list(TOOLS) + list(DOC_TOOLS), "approval_required_tools": []},
        workspace_root=tmp_path,
        tool_names=tool_names,
    )
    call = _call("excel_edit_tool", str(tmp_path / "data.xlsx"))

    assert _authorize(bridge, call) == AuthorizationStatus.REQUIRES_APPROVAL


def test_silent_mode_sandboxes_convert_tool_output(tmp_path):
    tool_names = list(TOOLS) + list(DOC_TOOLS)
    bridge, delegate, _ = _make_bridge(
        {
            "mode": "silent",
            "allowed_tools": list(WHITELIST) + list(DOC_TOOLS),
            "approval_required_tools": [],
        },
        workspace_root=tmp_path,
        tool_names=tool_names,
    )
    call = ToolCall(
        call_id="c1",
        name="document_convert_tool",
        arguments={
            "command": "docx_to_pdf",
            "input_path": str(tmp_path / "in.docx"),
            "output_path": str(tmp_path.parent / "out.pdf"),
        },
    )

    results = asyncio.run(bridge.sequential_tool_call([call]))

    assert not results[0].success
    assert "outside workspace" in (results[0].error or "")
    assert delegate.executed == []


def test_silent_mode_allows_relative_workspace_path(tmp_path):
    tool_names = list(TOOLS) + list(DOC_TOOLS)
    bridge, delegate, _ = _make_bridge(
        {
            "mode": "silent",
            "allowed_tools": list(WHITELIST) + list(DOC_TOOLS),
            "approval_required_tools": [],
        },
        workspace_root=tmp_path,
        tool_names=tool_names,
    )
    call = ToolCall(
        call_id="c1",
        name="str_replace_based_edit_tool",
        arguments={
            "command": "view",
            "path": ".agentsupport/skill-generation-abc/events.jsonl",
        },
    )

    results = asyncio.run(bridge.sequential_tool_call([call]))

    assert results[0].success
    assert len(delegate.executed) == 1


def test_ask_user_gate_pauses_and_returns_answer(tmp_path):
    tool_names = list(TOOLS) + ["ask_user"]
    bridge, delegate, emitted = _make_bridge(
        {
            "mode": "silent",
            "allowed_tools": list(WHITELIST) + ["ask_user"],
            "approval_required_tools": [],
        },
        workspace_root=tmp_path,
        tool_names=tool_names,
    )
    call = ToolCall(
        call_id="q1",
        name="ask_user",
        arguments={"question": "这样总结可以吗？"},
    )

    async def scenario():
        task = asyncio.create_task(bridge.sequential_tool_call([call]))
        for _ in range(200):
            if any(t == "interaction.requested" for t, _ in emitted):
                break
            await asyncio.sleep(0)
        requested = [
            payload for t, payload in emitted if t == "interaction.requested"
        ]
        assert requested and requested[-1]["kind"] == "question"
        assert requested[-1]["question"] == "这样总结可以吗？"
        bridge.submit_answer("同意，按此范围生成")
        return await task

    results = asyncio.run(scenario())

    assert results[0].success
    assert "同意，按此范围生成" in (results[0].result or "")
    assert delegate.executed == []


def test_ask_user_empty_question_returns_error_without_pause(tmp_path):
    tool_names = list(TOOLS) + ["ask_user"]
    bridge, delegate, emitted = _make_bridge(
        {
            "mode": "silent",
            "allowed_tools": list(WHITELIST) + ["ask_user"],
            "approval_required_tools": [],
        },
        workspace_root=tmp_path,
        tool_names=tool_names,
    )
    call = ToolCall(call_id="q1", name="ask_user", arguments={})

    results = asyncio.run(bridge.sequential_tool_call([call]))

    assert not results[0].success
    assert "缺少 question 参数" in (results[0].error or "")
    assert "场景" in (results[0].error or "")
    assert not [t for t, _ in emitted if t == "interaction.requested"]
    assert bridge._answer is None
    assert bridge.ask_answered is False
    assert delegate.executed == []


def test_ask_user_mixed_batch_fails_without_delegate_call(tmp_path):
    tool_names = list(TOOLS) + ["ask_user"]
    bridge, delegate, _ = _make_bridge(
        {
            "mode": "silent",
            "allowed_tools": list(WHITELIST) + ["ask_user"],
            "approval_required_tools": [],
        },
        workspace_root=tmp_path,
        tool_names=tool_names,
    )
    calls = [
        ToolCall(call_id="q1", name="ask_user", arguments={"question": "确认？"}),
        _call("str_replace_based_edit_tool", str(tmp_path / "events.jsonl")),
    ]

    async def scenario():
        try:
            await bridge.sequential_tool_call(calls)
        except RuntimeError as exc:
            return str(exc)
        return None

    message = asyncio.run(scenario())
    assert message and "ask_user must be called alone" in message
    assert delegate.executed == []


def test_generation_write_blocked_until_ask_user_answered(tmp_path):
    tool_names = list(TOOLS) + ["ask_user"]
    bridge, delegate, _ = _make_bridge(
        {
            "mode": "silent",
            "allowed_tools": list(WHITELIST) + ["ask_user"],
            "approval_required_tools": [],
        },
        workspace_root=tmp_path,
        tool_names=tool_names,
    )
    write_call = _call("str_replace_based_edit_tool", str(tmp_path / "SKILL.md"))
    write_call.arguments["command"] = "create"

    async def run_scenario():
        blocked = await bridge.sequential_tool_call([write_call])
        assert not blocked[0].success
        assert "ask_user" in (blocked[0].error or "")
        assert delegate.executed == []
        ask_call = ToolCall(
            call_id="q2",
            name="ask_user",
            arguments={"question": "确认范围？"},
        )
        task = asyncio.create_task(bridge.sequential_tool_call([ask_call]))
        for _ in range(200):
            if bridge._answer is not None:
                break
            await asyncio.sleep(0)
        bridge.submit_answer("同意，按此范围生成")
        await task
        assert bridge.ask_answered
        ok = await bridge.sequential_tool_call([write_call])
        assert ok[0].success
        assert len(delegate.executed) == 1

    asyncio.run(run_scenario())


def test_silent_mode_rejects_relative_path_escaping_workspace(tmp_path):
    tool_names = list(TOOLS) + list(DOC_TOOLS)
    bridge, delegate, _ = _make_bridge(
        {
            "mode": "silent",
            "allowed_tools": list(WHITELIST) + list(DOC_TOOLS),
            "approval_required_tools": [],
        },
        workspace_root=tmp_path,
        tool_names=tool_names,
    )
    call = ToolCall(
        call_id="c1",
        name="str_replace_based_edit_tool",
        arguments={"command": "view", "path": "../outside.jsonl"},
    )

    results = asyncio.run(bridge.sequential_tool_call([call]))

    assert not results[0].success
    assert "outside workspace" in (results[0].error or "")
    assert delegate.executed == []

"""Conversation-level execution modes flow into the run request tool policy.

Runs on the temporal-only core via the test-side embedded coordinator
(``tests/_support.py``); no Temporal server and no model are required.
"""

from __future__ import annotations

from agentsupport.domain import ConversationMode

from _support import make_temporal_service

SILENT_TOOLS = [
    "str_replace_based_edit_tool",
    "json_edit_tool",
    "word_edit_tool",
    "excel_edit_tool",
    "pdf_tool",
    "document_convert_tool",
    "task_done",
]


async def _session(env):
    workspace = env.service.create_workspace("ws")
    return env.service.create_session(workspace.id, tenant_id="t-1", name="s")


async def _policy_after_run(env, conversation):
    await env.coordinator.wait_for_run(str(conversation.run.run_id), timeout_seconds=15)
    assert env.fake.captured, "runner never received the run request"
    return env.fake.captured[0]["tool_policy"]


async def test_silent_mode_policy_in_run_request(tmp_path):
    env = make_temporal_service(tmp_path)
    session = await _session(env)

    conversation = await env.service.create_conversation(
        session.id, "task", mode=ConversationMode.SILENT
    )

    policy = await _policy_after_run(env, conversation)
    assert conversation.mode == ConversationMode.SILENT
    assert policy["mode"] == "silent"
    assert policy["allowed_tools"] == SILENT_TOOLS
    assert policy["approval_required_tools"] == []


async def test_no_approval_mode_policy_in_run_request(tmp_path):
    env = make_temporal_service(tmp_path)
    session = await _session(env)

    conversation = await env.service.create_conversation(
        session.id, "task", mode=ConversationMode.NO_APPROVAL
    )

    policy = await _policy_after_run(env, conversation)
    assert policy["mode"] == "no_approval"
    assert policy["approval_required_tools"] == []


async def test_default_mode_policy_unmarked(tmp_path):
    env = make_temporal_service(tmp_path)
    session = await _session(env)

    conversation = await env.service.create_conversation(session.id, "task")

    policy = await _policy_after_run(env, conversation)
    assert "mode" not in policy

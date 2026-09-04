"""Unit tests for skill generation operations on the temporal-only core.

Generation runs through the test-side embedded coordinator; completion is
observed by polling ``get_skill_generation`` after the run reaches a terminal
state (the same contract the demo uses against a real deployment).
"""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from _support import make_temporal_service as _make_service

from agent_runner_contracts.events import EventEnvelope
from agentsupport.application.service import ServiceError
from agentsupport.domain import ConversationMode, DraftStatus, GenerationStatus
from agentsupport.skills import LocalSkillProvider

SKILL_MARKDOWN = """---
name: fix-n-plus-one
description: 修复 SQLAlchemy 列表查询 N+1 的标准路径
---

# 目标

消除列表接口的 N+1 查询。

# 步骤

1. 定位循环内查询。
2. 改为批量加载。

# 边界

仅适用于 SQLAlchemy 2.x。
"""


def _env(tmp_path, **overrides):
    kwargs = {
        "workspace_root": tmp_path / "workspaces",
        "skills_root": tmp_path / "skills",
    }
    kwargs.update(overrides)
    return _make_service(tmp_path, **kwargs)


async def _completed_session(env, *, tenant_id="t-1"):
    service = env.service
    workspace = service.create_workspace("demo")
    session = service.create_session(workspace.id, tenant_id=tenant_id, project_id="p-1")
    conversation = await service.create_conversation(session.id, "business task")
    await env.coordinator.wait_for_run(str(conversation.run.run_id), timeout_seconds=20)
    return session


async def _generate(env, session, *, tenant_id="t-1"):
    service = env.service
    request = await service.generate_skill(session.id, tenant_id=tenant_id)
    conversation = service.repository.get_conversation(request.conversation_id)
    if conversation is not None:
        await env.coordinator.wait_for_run(str(conversation.run.run_id), timeout_seconds=30)
    return service.get_skill_generation(request.id, tenant_id=tenant_id)


async def test_generate_skill_creates_draft_and_completes(tmp_path):
    env = _env(tmp_path, runner_result={"skill_markdown": SKILL_MARKDOWN})
    session = await _completed_session(env)

    request = await _generate(env, session)

    assert request.status == GenerationStatus.COMPLETED
    assert request.conversation_id is not None
    assert request.tenant_id == "t-1"


async def test_generate_skill_writes_events_to_runner_workspace_root(tmp_path):
    runner_root = tmp_path / "runner-workspace"
    env = _env(
        tmp_path,
        runner_workspace_root=runner_root,
        runner_result={"skill_markdown": SKILL_MARKDOWN},
    )
    session = await _completed_session(env)

    request = await _generate(env, session)

    event_files = list(runner_root.rglob("events.jsonl"))
    assert event_files, "events.jsonl not written under runner_workspace_root"
    assert str(request.id) in event_files[0].as_posix()
    control_plane_files = list((tmp_path / "workspaces").rglob("events.jsonl"))
    assert not control_plane_files, "events.jsonl must not be written to control-plane workspace"
    drafts = env.service.list_skill_drafts(tenant_id="t-1")
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.status == DraftStatus.DRAFT
    assert draft.skill_id == "fix-n-plus-one"
    assert draft.tenant_id == "t-1"
    assert draft.project_id == "p-1"
    assert draft.frontmatter["name"] == "fix-n-plus-one"
    assert draft.source_session_id == session.id


async def test_generate_skill_conversation_uses_silent_mode(tmp_path):
    env = _env(tmp_path, runner_result={"skill_markdown": SKILL_MARKDOWN})
    session = await _completed_session(env)

    request = await _generate(env, session)

    conversation = env.service.repository.get_conversation(request.conversation_id)
    assert conversation is not None
    assert conversation.mode == ConversationMode.SILENT


async def test_generate_skill_run_allows_ask_user_confirmation_tool(tmp_path):
    env = _env(tmp_path, runner_result={"skill_markdown": SKILL_MARKDOWN})
    session = await _completed_session(env)

    await _generate(env, session)

    policy = env.fake.captured[-1]["tool_policy"]
    assert policy["mode"] == "silent"
    assert "ask_user" in policy["allowed_tools"]


async def test_generate_skill_unwraps_json_string_result(tmp_path):
    env = _env(
        tmp_path,
        runner_result={
            "status": "completed",
            "content": json.dumps({"skill_markdown": SKILL_MARKDOWN}),
            "steps": 1,
            "usage": {},
        },
    )
    session = await _completed_session(env)

    request = await _generate(env, session)

    assert request.status == GenerationStatus.COMPLETED
    draft = env.service.list_skill_drafts(tenant_id="t-1")[0]
    assert draft.skill_id == "fix-n-plus-one"
    assert draft.frontmatter["name"] == "fix-n-plus-one"


async def test_generate_skill_unwraps_fenced_json_result(tmp_path):
    fenced = "```json\n" + json.dumps({"skill_markdown": SKILL_MARKDOWN}) + "\n```"
    env = _env(
        tmp_path,
        runner_result={
            "status": "completed",
            "content": fenced,
            "steps": 1,
            "usage": {},
        },
    )
    session = await _completed_session(env)

    request = await _generate(env, session)

    assert request.status == GenerationStatus.COMPLETED
    draft = env.service.list_skill_drafts(tenant_id="t-1")[0]
    assert draft.skill_id == "fix-n-plus-one"
    assert draft.frontmatter["name"] == "fix-n-plus-one"


async def test_generate_skill_requires_matching_tenant(tmp_path):
    env = _env(tmp_path)
    session = await _completed_session(env, tenant_id="t-1")

    with pytest.raises(ServiceError) as exc:
        await env.service.generate_skill(session.id, tenant_id="t-2")
    assert exc.value.status_code == 404


async def test_generate_skill_session_not_found(tmp_path):
    env = _env(tmp_path)

    with pytest.raises(ServiceError) as exc:
        await env.service.generate_skill(UUID(int=1), tenant_id="t-1")
    assert exc.value.status_code == 404


async def test_invalid_frontmatter_marks_generation_failed(tmp_path):
    env = _env(tmp_path, runner_result={"skill_markdown": "# no frontmatter here"})
    session = await _completed_session(env)

    request = await _generate(env, session)

    assert request.status == GenerationStatus.FAILED
    assert "frontmatter" in (request.error or "")
    assert env.service.list_skill_drafts(tenant_id="t-1") == []


async def test_generation_task_failure_marks_generation_failed(tmp_path):
    env = _env(tmp_path)
    service = env.service
    workspace = service.create_workspace("demo")
    session = service.create_session(workspace.id, tenant_id="t-1")

    class _FailingCore:
        async def run(self, request: dict, event_sink):
            await event_sink(
                EventEnvelope(
                    run_id=UUID(request["run_id"]),
                    seq=1,
                    type="run.failed",
                    payload={"code": "BOOM", "message": "model unavailable"},
                    source="runner",
                )
            )
            return {"status": "RUNNING", "events": []}

    env.coordinator._ctx.core_runtime = _FailingCore()  # type: ignore[assignment]
    request = await _generate(env, session)
    assert request.status == GenerationStatus.FAILED
    assert request.error == "generation run ended with FAILED"


async def test_review_approve_publishes_into_tenant_namespace(tmp_path):
    skills_root = tmp_path / "skills"
    env = _env(
        tmp_path,
        skills_root=skills_root,
        runner_result={"skill_markdown": SKILL_MARKDOWN},
    )
    session = await _completed_session(env)
    await _generate(env, session)
    draft = env.service.list_skill_drafts(tenant_id="t-1")[0]

    published = env.service.review_skill_draft(
        draft.id, tenant_id="t-1", decision="approve", note="ok"
    )

    assert published.status == DraftStatus.PUBLISHED
    assert published.review_note == "ok"
    provider = LocalSkillProvider(skills_root)
    tenant_skills = provider.list_skills(tenant_id="t-1")
    assert [item["skill_id"] for item in tenant_skills] == ["fix-n-plus-one"]
    assert provider.list_skills() == []  # global namespace untouched
    assert (skills_root / "tenants" / "t-1" / "fix-n-plus-one" / "SKILL.md").is_file()

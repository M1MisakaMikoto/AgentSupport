"""Unit tests for skill generation operations (state machine, review, tenants)."""

import json
from uuid import UUID

import pytest

from agent_runner_contracts.events import EventEnvelope
from agentsupport.application.service import ServiceError
from agentsupport.config import Settings
from agentsupport.domain import DraftStatus, GenerationStatus
from agentsupport.services import AgentSupportService
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


class _CompletedCore:
    def __init__(self, result):
        self.result = result

    async def run(self, request: dict, event_sink):
        await event_sink(
            EventEnvelope(
                run_id=UUID(request["run_id"]),
                seq=1,
                type="run.completed",
                payload={"result": self.result},
                source="runner",
            )
        )
        return {"status": "RUNNING", "events": []}

    def register_run_endpoint(self, run_id, endpoint):
        return None


class _FakeRuntimeDriver:
    def __init__(self):
        self.mounts: list[tuple[str, str]] = []

    async def start(
        self,
        session_id,
        workspace_path,
        lease_epoch,
        workspace_id=None,
        read_only_mounts=None,
        runtime_operation_id=None,
    ):
        self.mounts = list(read_only_mounts or [])
        return f"container-{session_id}"

    async def stop(self, container_id, *, force=False):
        return True

    async def inspect(self, container_id):
        return {"read_only_mounts": self.mounts}

    async def endpoint(self, container_id):
        return None


def _service(tmp_path, *, result=None, skills_root=None):
    service = AgentSupportService(
        Settings(
            workspace_root=tmp_path / "workspaces",
            skills_root=skills_root or tmp_path / "skills",
        )
    )
    service.core_runtime = _CompletedCore(
        result if result is not None else {"skill_markdown": SKILL_MARKDOWN}
    )
    service.runtime_driver = _FakeRuntimeDriver()
    return service


async def _completed_session(service, *, tenant_id="t-1"):
    workspace = service.create_workspace("demo")
    session = service.create_session(workspace.id, tenant_id=tenant_id, project_id="p-1")
    await service.create_conversation(session.id, "business task")
    return session


async def test_generate_skill_creates_draft_and_completes(tmp_path):
    service = _service(tmp_path)
    session = await _completed_session(service)

    request = await service.generate_skill(session.id, tenant_id="t-1")

    assert request.status == GenerationStatus.COMPLETED
    assert request.conversation_id is not None
    assert request.tenant_id == "t-1"
    drafts = service.list_skill_drafts(tenant_id="t-1")
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.status == DraftStatus.DRAFT
    assert draft.skill_id == "fix-n-plus-one"
    assert draft.tenant_id == "t-1"
    assert draft.project_id == "p-1"
    assert draft.frontmatter["name"] == "fix-n-plus-one"
    assert draft.source_session_id == session.id


async def test_generate_skill_unwraps_json_string_result(tmp_path):
    """Real runner shape: result.content is a JSON string holding skill_markdown."""
    service = _service(
        tmp_path,
        result={
            "status": "completed",
            "content": json.dumps({"skill_markdown": SKILL_MARKDOWN}),
            "steps": 1,
            "usage": {},
        },
    )
    session = await _completed_session(service)

    request = await service.generate_skill(session.id, tenant_id="t-1")

    assert request.status == GenerationStatus.COMPLETED
    draft = service.list_skill_drafts(tenant_id="t-1")[0]
    assert draft.skill_id == "fix-n-plus-one"
    assert draft.frontmatter["name"] == "fix-n-plus-one"


async def test_generate_skill_unwraps_fenced_json_result(tmp_path):
    """Real models often wrap the JSON envelope in a ```json code block."""
    fenced = "```json\n" + json.dumps({"skill_markdown": SKILL_MARKDOWN}) + "\n```"
    service = _service(
        tmp_path,
        result={"status": "completed", "content": fenced, "steps": 1, "usage": {}},
    )
    session = await _completed_session(service)

    request = await service.generate_skill(session.id, tenant_id="t-1")

    assert request.status == GenerationStatus.COMPLETED
    draft = service.list_skill_drafts(tenant_id="t-1")[0]
    assert draft.skill_id == "fix-n-plus-one"
    assert draft.frontmatter["name"] == "fix-n-plus-one"

async def test_generate_skill_requires_matching_tenant(tmp_path):
    service = _service(tmp_path)
    session = await _completed_session(service, tenant_id="t-1")

    with pytest.raises(ServiceError) as exc:
        await service.generate_skill(session.id, tenant_id="t-2")
    assert exc.value.status_code == 404


async def test_generate_skill_session_not_found(tmp_path):
    service = _service(tmp_path)

    with pytest.raises(ServiceError) as exc:
        await service.generate_skill(UUID(int=1), tenant_id="t-1")
    assert exc.value.status_code == 404


async def test_invalid_frontmatter_marks_generation_failed(tmp_path):
    service = _service(tmp_path, result={"skill_markdown": "# no frontmatter here"})
    session = await _completed_session(service)

    request = await service.generate_skill(session.id, tenant_id="t-1")

    assert request.status == GenerationStatus.FAILED
    assert "frontmatter" in (request.error or "")
    assert service.list_skill_drafts(tenant_id="t-1") == []


async def test_generation_task_failure_marks_generation_failed(tmp_path):
    service = _service(tmp_path)
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

        def register_run_endpoint(self, run_id, endpoint):
            return None

    service.core_runtime = _FailingCore()
    request = await service.generate_skill(session.id, tenant_id="t-1")
    assert request.status == GenerationStatus.FAILED
    assert request.error == "generation run ended with FAILED"


async def test_review_approve_publishes_into_tenant_namespace(tmp_path):
    skills_root = tmp_path / "skills"
    service = _service(tmp_path, skills_root=skills_root)
    session = await _completed_session(service)
    await service.generate_skill(session.id, tenant_id="t-1")
    draft = service.list_skill_drafts(tenant_id="t-1")[0]

    published = service.review_skill_draft(
        draft.id, tenant_id="t-1", decision="approve", note="ok"
    )

    assert published.status == DraftStatus.PUBLISHED
    assert published.review_note == "ok"
    provider = LocalSkillProvider(skills_root)
    tenant_skills = provider.list_skills(tenant_id="t-1")
    assert [item["skill_id"] for item in tenant_skills] == ["fix-n-plus-one"]
    assert provider.list_skills() == []  # global namespace untouched
    assert (skills_root / "tenants" / "t-1" / "fix-n-plus-one" / "SKILL.md").is_file()


async def test_review_reject_marks_rejected_without_install(tmp_path):
    skills_root = tmp_path / "skills"
    service = _service(tmp_path, skills_root=skills_root)
    session = await _completed_session(service)
    await service.generate_skill(session.id, tenant_id="t-1")
    draft = service.list_skill_drafts(tenant_id="t-1")[0]

    rejected = service.review_skill_draft(
        draft.id, tenant_id="t-1", decision="reject", note="not generic enough"
    )

    assert rejected.status == DraftStatus.REJECTED
    provider = LocalSkillProvider(skills_root)
    assert provider.list_skills(tenant_id="t-1") == []


async def test_review_twice_conflicts(tmp_path):
    service = _service(tmp_path)
    session = await _completed_session(service)
    await service.generate_skill(session.id, tenant_id="t-1")
    draft = service.list_skill_drafts(tenant_id="t-1")[0]
    service.review_skill_draft(draft.id, tenant_id="t-1", decision="approve")

    with pytest.raises(ServiceError) as exc:
        service.review_skill_draft(draft.id, tenant_id="t-1", decision="reject")
    assert exc.value.status_code == 409


async def test_review_requires_matching_tenant(tmp_path):
    service = _service(tmp_path)
    session = await _completed_session(service)
    await service.generate_skill(session.id, tenant_id="t-1")
    draft = service.list_skill_drafts(tenant_id="t-1")[0]

    with pytest.raises(ServiceError) as exc:
        service.review_skill_draft(draft.id, tenant_id="t-2", decision="approve")
    assert exc.value.status_code == 404


async def test_generation_query_respects_tenant(tmp_path):
    service = _service(tmp_path)
    session = await _completed_session(service)
    request = await service.generate_skill(session.id, tenant_id="t-1")

    fetched = service.get_skill_generation(request.id, tenant_id="t-1")
    assert fetched.status == GenerationStatus.COMPLETED

    with pytest.raises(ServiceError) as exc:
        service.get_skill_generation(request.id, tenant_id="t-2")
    assert exc.value.status_code == 404

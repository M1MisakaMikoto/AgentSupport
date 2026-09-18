"""Tenant-scoped skill upload: isolation, fallback and session resolution."""

from __future__ import annotations

import pytest
from _support import make_temporal_service as _make_service
from httpx import ASGITransport, AsyncClient

from agentsupport.api import create_app
from agentsupport.application.service import ServiceError
from agentsupport.domain import PresetSkill, ProjectConfig

SKILL_MD = (
    "---\nname: mytool\ndescription: use mytool\n---\n\n# mytool\n\nmytool list\n"
)


def _service(tmp_path, **overrides):
    return _make_service(
        tmp_path,
        workspace_root=tmp_path / "workspaces",
        skills_root=tmp_path / "skills",
        presets_root=tmp_path / "presets",
        **overrides,
    ).service


def test_tenant_skill_is_isolated_and_overrides_shared(tmp_path):
    service = _service(tmp_path)
    service.create_skill(
        "shared-tool", filename="SKILL.md", payload=SKILL_MD.encode("utf-8")
    )
    service.create_skill(
        "acme-tool",
        filename="SKILL.md",
        payload=SKILL_MD.replace("mytool", "acme-tool").encode("utf-8"),
        tenant_id="acme",
    )
    service.create_skill(
        "other-tool",
        filename="SKILL.md",
        payload=SKILL_MD.replace("mytool", "other").encode("utf-8"),
        tenant_id="other",
    )

    acme = {item["skill_id"] for item in service.list_skills(tenant_id="acme")}
    other = {item["skill_id"] for item in service.list_skills(tenant_id="other")}
    shared = {item["skill_id"] for item in service.list_skills()}

    assert acme == {"acme-tool", "shared-tool"}
    assert other == {"other-tool", "shared-tool"}
    assert shared == {"shared-tool"}

    # A tenant skill shadows a shared skill with the same id.
    service.create_skill(
        "shared-tool",
        filename="SKILL.md",
        payload=SKILL_MD.replace("mytool", "tenant-version").encode("utf-8"),
        tenant_id="acme",
    )
    resolved = service.get_skill("shared-tool", tenant_id="acme")
    assert resolved["skill_id"] == "shared-tool"
    assert (tmp_path / "skills" / "tenants" / "acme" / "shared-tool" / "SKILL.md").is_file()

    service.delete_skill("shared-tool", tenant_id="acme")
    assert service.get_skill("shared-tool")["skill_id"] == "shared-tool"
    with pytest.raises(ServiceError) as exc:
        service.delete_skill("acme-tool", tenant_id="other")
    assert exc.value.code == "SKILL_NOT_FOUND"


def test_preset_skills_reach_the_session_candidate_pool(tmp_path):
    service = _service(tmp_path)
    service.create_skill(
        "acme-tool",
        filename="SKILL.md",
        payload=SKILL_MD.replace("mytool", "acme-tool").encode("utf-8"),
        tenant_id="acme",
    )
    service.put_tenant_preset(
        "acme",
        metadata={
            "name": "acme",
            "cli_apps": [
                {
                    "cli_id": "mytool",
                    "entry": "mytool",
                    "package": "mytool.tar.gz",
                }
            ],
            "skills": ["acme-tool"],
        },
        files={"mytool.tar.gz": b"payload"},
    )

    workspace = service.create_workspace("ws")
    session = service.create_session(workspace.id, tenant_id="acme")
    assert service._skills_for_session(session) == ["acme-tool"]

    # An explicit session skill list always wins over the preset.
    explicit = service.create_session(
        workspace.id,
        tenant_id="acme",
        config=ProjectConfig(skills=[PresetSkill(skill_id="acme-tool")]),
    )
    assert service._skills_for_session(explicit) == ["acme-tool"]

    # The preset must reference skills that exist for that tenant.
    with pytest.raises(ServiceError) as exc:
        service.put_tenant_preset(
            "other",
            metadata={
                "cli_apps": [
                    {
                        "cli_id": "mytool",
                        "entry": "mytool",
                        "package": "mytool.tar.gz",
                    }
                ],
                "skills": ["acme-tool"],
            },
            files={"mytool.tar.gz": b"payload"},
        )
    assert exc.value.code == "SKILL_NOT_FOUND"


@pytest.mark.asyncio
async def test_skills_api_supports_tenant_scope(tmp_path):
    service = _service(tmp_path)
    app = create_app(service)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        uploaded = await client.post(
            "/skills",
            data={"skill_id": "acme-tool", "tenant_id": "acme"},
            files={"file": ("SKILL.md", SKILL_MD.encode("utf-8"), "text/markdown")},
        )
        assert uploaded.status_code == 201, uploaded.text

        scoped = await client.get("/skills", params={"tenant_id": "acme"})
        assert [item["skill_id"] for item in scoped.json()] == ["acme-tool"]

        unscoped = await client.get("/skills")
        assert unscoped.json() == []

        headered = await client.get("/skills", headers={"X-Tenant-Id": "acme"})
        assert [item["skill_id"] for item in headered.json()] == ["acme-tool"]

        removed = await client.delete("/skills/acme-tool", params={"tenant_id": "acme"})
        assert removed.status_code == 204
        assert (await client.get("/skills", params={"tenant_id": "acme"})).json() == []

"""Skill self-service API and provider tests."""

import io
import zipfile

import pytest
from httpx import ASGITransport, AsyncClient

from agentsupport.api import create_app
from agentsupport.application.service import ServiceError
from agentsupport.config import Settings
from agentsupport.services import AgentSupportService
from agentsupport.skills import LocalSkillProvider


def _zip(payload: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in payload.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _service(tmp_path):
    return AgentSupportService(
        Settings(
            workspace_root=tmp_path / "workspaces",
            skills_root=tmp_path / "skills",
            enabled_skills="",
        )
    )


def test_provider_install_list_describe_remove(tmp_path):
    provider = LocalSkillProvider(tmp_path / "skills")
    installed = provider.install_skill(
        "review", {"SKILL.md": b"# Review\n", "references/guide.md": b"guide"}
    )
    assert installed["skill_id"] == "review"
    assert installed["mount_path"] == "/opt/agent-skills/review"
    assert len(installed["content_hash"]) == 64

    listed = provider.list_skills()
    assert [item["skill_id"] for item in listed] == ["review"]
    detail = provider.describe_skill("review")
    assert {item["path"] for item in detail["files"]} == {
        "SKILL.md",
        "references/guide.md",
    }
    assert provider.remove_skill("review") is True
    assert provider.remove_skill("review") is False


def test_provider_overwrite_updates_content_hash(tmp_path):
    provider = LocalSkillProvider(tmp_path / "skills")
    first = provider.install_skill("review", {"SKILL.md": b"v1"})
    second = provider.install_skill("review", {"SKILL.md": b"v2"})
    assert first["content_hash"] != second["content_hash"]


def test_provider_skill_prompt_entries_carries_content_and_truncates(tmp_path):
    provider = LocalSkillProvider(tmp_path / "skills")
    long_skill = "# Review\n" + ("x" * 5000)
    provider.install_skill("review", {"SKILL.md": long_skill.encode()})

    entries = provider.skill_prompt_entries(["review"], max_chars=100)

    assert len(entries) == 1
    assert entries[0]["skill_id"] == "review"
    assert entries[0]["mount_path"] == "/opt/agent-skills/review"
    assert entries[0]["truncated"] is True
    assert entries[0]["content"].startswith("# Review\n")
    assert "已截断" in entries[0]["content"]

    short = provider.skill_prompt_entries(["review"], max_chars=20000)
    assert short[0]["truncated"] is False
    assert short[0]["content"] == long_skill


@pytest.mark.parametrize(
    "payload,error",
    [
        ({"other.md": b"x"}, "SKILL.md"),
        ({"../evil.md": b"x", "SKILL.md": b"# ok"}, "escapes"),
    ],
)
def test_provider_rejects_bad_packages(tmp_path, payload, error):
    provider = LocalSkillProvider(tmp_path / "skills")
    with pytest.raises(ValueError, match=error):
        provider.install_zip("bad", _zip(payload))
    with pytest.raises(ValueError, match="invalid skill id"):
        provider.install_skill("../escape", {"SKILL.md": b"# x"})


@pytest.mark.asyncio
async def test_skills_api_upload_list_detail_delete(tmp_path):
    service = _service(tmp_path)
    app = create_app(service)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/skills",
            data={"skill_id": "review"},
            files={"file": ("SKILL.md", b"# Review\n", "text/markdown")},
        )
        assert created.status_code == 201, created.text
        assert created.json()["skill_id"] == "review"

        listed = await client.get("/skills")
        assert listed.status_code == 200
        assert [item["skill_id"] for item in listed.json()] == ["review"]

        detail = await client.get("/skills/review")
        assert detail.status_code == 200
        assert detail.json()["files"][0]["path"] == "SKILL.md"

        removed = await client.delete("/skills/review")
        assert removed.status_code == 204
        gone = await client.get("/skills/review")
        assert gone.status_code == 404


@pytest.mark.asyncio
async def test_skills_api_accepts_zip_and_rejects_bad_packages(tmp_path):
    service = _service(tmp_path)
    app = create_app(service)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        good = await client.post(
            "/skills",
            data={"skill_id": "review"},
            files={
                "file": (
                    "review.zip",
                    _zip({"SKILL.md": b"# Review", "guide.md": b"guide"}),
                    "application/zip",
                )
            },
        )
        assert good.status_code == 201, good.text

        missing_md = await client.post(
            "/skills",
            data={"skill_id": "broken"},
            files={"file": ("broken.zip", _zip({"readme.md": b"x"}), "application/zip")},
        )
        assert missing_md.status_code == 422
        assert missing_md.json()["code"] == "SKILL_INVALID_PAYLOAD"

        traversal = await client.post(
            "/skills",
            data={"skill_id": "evil"},
            files={
                "file": (
                    "evil.zip",
                    _zip({"../evil.md": b"x", "SKILL.md": b"# ok"}),
                    "application/zip",
                )
            },
        )
        assert traversal.status_code == 422

        invalid_id = await client.post(
            "/skills",
            data={"skill_id": "../escape"},
            files={"file": ("SKILL.md", b"# x", "text/markdown")},
        )
        assert invalid_id.status_code == 422


def test_session_create_validates_skill_references(tmp_path):
    service = _service(tmp_path)
    service.create_skill("review", filename="SKILL.md", payload=b"# Review\n")
    from agentsupport.domain import PresetSkill, ProjectConfig

    workspace = service.create_workspace("skills")
    session = service.create_session(
        workspace.id,
        config=ProjectConfig(skills=[PresetSkill(skill_id="review", enabled=True)]),
    )
    assert session.id is not None

    with pytest.raises(ServiceError) as exc:
        service.create_session(
            workspace.id,
            config=ProjectConfig(skills=[PresetSkill(skill_id="missing", enabled=True)]),
        )
    assert exc.value.code == "SKILL_NOT_FOUND"
    assert exc.value.status_code == 404

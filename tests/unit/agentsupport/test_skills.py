from pathlib import Path

import pytest

from agentsupport.config import Settings
from agentsupport.services import AgentSupportService
from agentsupport.skills import LocalSkillProvider
from agentsupport.storage import LocalWorkspaceStorageDriver


def test_authorized_skill_manifest_and_read_only_mount(tmp_path):
    skills_root = tmp_path / "skills"
    skill = skills_root / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Review\n", encoding="utf-8")

    provider = LocalSkillProvider(skills_root)
    manifest = provider.manifest(["review"])
    mounts = provider.read_only_mounts(["review"])
    assert manifest[0]["skill_id"] == "review"
    assert manifest[0]["mount_path"] == "/opt/agent-skills/review"
    assert len(manifest[0]["content_hash"]) == 64
    assert mounts == [(str(skill.resolve()), "/opt/agent-skills/review")]


async def test_session_runtime_receives_only_authorized_skill_mounts(tmp_path):
    skills_root = tmp_path / "skills"
    skill = skills_root / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Review\n", encoding="utf-8")
    service = AgentSupportService(
        Settings(
            workspace_root=tmp_path / "workspaces",
            skills_root=skills_root,
            enabled_skills="review",
        )
    )
    workspace = service.create_workspace("skills")
    session = service.create_session(workspace.id)
    await service.create_conversation(session.id, "run")
    inspection = await service.runtime_driver.inspect(session.active_container_id)
    assert inspection["read_only_mounts"] == [(str(skill.resolve()), "/opt/agent-skills/review")]


def test_local_workspace_storage_driver_creates_and_restores_versions(tmp_path):
    driver = LocalWorkspaceStorageDriver(tmp_path / "workspaces")
    workspace_id, workspace_path = driver.create("versioned")
    root = Path(workspace_path)
    (root / "file.txt").write_text("v1", encoding="utf-8")

    assert driver.path(workspace_id) == workspace_path
    version_id = driver.create_version(workspace_id, name="baseline")
    assert [item["version_id"] for item in driver.list_versions(workspace_id)] == [
        version_id
    ]

    (root / "file.txt").write_text("v2", encoding="utf-8")
    driver.restore_version(workspace_id, version_id)
    assert (root / "file.txt").read_text(encoding="utf-8") == "v1"

    with pytest.raises(FileNotFoundError):
        driver.restore_version(workspace_id, "missing-version")

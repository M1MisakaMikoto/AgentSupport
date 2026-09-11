import base64
from pathlib import Path

import pytest

from agentsupport.skills import LocalSkillProvider
from agentsupport.storage import LocalWorkspaceStorageDriver

SKILL_MD = """---
name: review
description: 输出审查报告
---

# Review
"""


def test_skill_catalog_carries_only_id_name_and_description(tmp_path):
    skills_root = tmp_path / "skills"
    skill = skills_root / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")

    provider = LocalSkillProvider(skills_root)

    assert provider.skill_catalog(["review"]) == [
        {"skill_id": "review", "name": "review", "description": "输出审查报告"}
    ]


def test_skill_package_carries_whole_directory(tmp_path):
    skills_root = tmp_path / "skills"
    skill = skills_root / "review"
    (skill / "references").mkdir(parents=True)
    # write_bytes keeps the fixture byte-identical (write_text would add CRLF on Windows)
    (skill / "SKILL.md").write_bytes(SKILL_MD.encode("utf-8"))
    (skill / "references" / "guide.md").write_text("guide", encoding="utf-8")

    package = LocalSkillProvider(skills_root).skill_package(["review"])

    assert package[0]["skill_id"] == "review"
    files = {entry["path"]: entry["content_b64"] for entry in package[0]["files"]}
    assert set(files) == {"SKILL.md", "references/guide.md"}
    assert base64.b64decode(files["references/guide.md"]) == b"guide"
    assert base64.b64decode(files["SKILL.md"]).decode("utf-8") == SKILL_MD


def test_install_requires_frontmatter(tmp_path):
    provider = LocalSkillProvider(tmp_path / "skills")

    with pytest.raises(ValueError):
        provider.install_skill("review", {"SKILL.md": b"# no frontmatter\n"})
    with pytest.raises(ValueError):
        provider.install_skill(
            "review", {"SKILL.md": b"---\nname: review\n---\n\n# Review\n"}
        )

    installed = provider.install_skill("review", {"SKILL.md": SKILL_MD.encode()})
    assert len(installed["content_hash"]) == 64


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

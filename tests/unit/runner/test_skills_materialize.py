"""Materialization: layout, read-only guarantee, traversal rejection, cleanup."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest

from session_runner.skills_materialize import (
    cleanup_skill_package,
    materialize_skill_package,
)


def _package(skill_id: str = "review", path: str = "SKILL.md", body: bytes = b"body"):
    return [
        {
            "skill_id": skill_id,
            "files": [{"path": path, "content_b64": base64.b64encode(body).decode()}],
        }
    ]


def test_materializes_each_skill_as_a_subdirectory(tmp_path: Path):
    root = materialize_skill_package(
        [
            *_package("review", "SKILL.md", b"# review"),
            *_package("docs", "references/checklist.md", b"- item"),
        ],
        tmp_path / "agent-skills" / "run-1",
    )

    assert (root / "review" / "SKILL.md").read_bytes() == b"# review"
    assert (root / "docs" / "references" / "checklist.md").read_bytes() == b"- item"


def test_materialized_tree_is_read_only(tmp_path: Path):
    root = materialize_skill_package(_package(), tmp_path / "agent-skills" / "run-1")
    skill_md = root / "review" / "SKILL.md"

    assert not os.access(skill_md, os.W_OK)
    with pytest.raises(OSError):
        skill_md.write_bytes(b"tampered")


def test_traversal_entries_are_rejected(tmp_path: Path):
    for bad in ("../escape.md", "/etc/passwd"):
        with pytest.raises(ValueError):
            materialize_skill_package(
                _package(path=bad), tmp_path / "agent-skills" / "run-1"
            )


def test_bad_skill_id_is_rejected(tmp_path: Path):
    with pytest.raises(ValueError):
        materialize_skill_package(_package(skill_id="../x"), tmp_path / "agent-skills" / "run-1")


def test_cleanup_removes_a_read_only_tree(tmp_path: Path):
    root = materialize_skill_package(_package(), tmp_path / "agent-skills" / "run-1")
    assert root.exists()

    cleanup_skill_package(root)

    assert not root.exists()


def test_rematerializing_replaces_previous_contents(tmp_path: Path):
    target = tmp_path / "agent-skills" / "run-1"
    materialize_skill_package(_package("old"), target)

    root = materialize_skill_package(_package("new"), target)

    assert not (root / "old").exists()
    assert (root / "new" / "SKILL.md").exists()

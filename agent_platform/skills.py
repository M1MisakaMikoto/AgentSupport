from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class SkillManifestEntry(BaseModel):
    skill_id: str
    content_hash: str
    mount_path: str


class LocalSkillProvider:
    """Authorized local Skill source with deterministic read-only mount metadata."""

    _valid_id = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def resolve(self, skill_ids: list[str]) -> list[tuple[SkillManifestEntry, Path]]:
        resolved: list[tuple[SkillManifestEntry, Path]] = []
        for skill_id in skill_ids:
            if not self._valid_id.fullmatch(skill_id):
                raise ValueError(f"invalid skill id: {skill_id}")
            source = (self.root / skill_id).resolve()
            if source.parent != self.root:
                raise ValueError(f"skill escapes configured root: {skill_id}")
            skill_file = source / "SKILL.md"
            if not skill_file.is_file():
                raise FileNotFoundError(f"authorized skill is missing SKILL.md: {skill_id}")
            digest = hashlib.sha256(skill_file.read_bytes()).hexdigest()
            resolved.append(
                (
                    SkillManifestEntry(
                        skill_id=skill_id,
                        content_hash=digest,
                        mount_path=f"/opt/agent-skills/{skill_id}",
                    ),
                    source,
                )
            )
        return resolved

    def manifest(self, skill_ids: list[str]) -> list[dict[str, Any]]:
        return [entry.model_dump(mode="json") for entry, _ in self.resolve(skill_ids)]

    def read_only_mounts(self, skill_ids: list[str]) -> list[tuple[str, str]]:
        return [(str(source), entry.mount_path) for entry, source in self.resolve(skill_ids)]

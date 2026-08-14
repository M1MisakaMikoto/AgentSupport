from __future__ import annotations

import hashlib
import re
import shutil
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

MAX_UPLOAD_SIZE = 2 * 1024 * 1024
MAX_ENTRY_SIZE = 2 * 1024 * 1024
MAX_UNPACKED_SIZE = 10 * 1024 * 1024
MAX_ENTRY_COUNT = 200
MAX_SKILL_PROMPT_CHARS = 20000


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
            resolved.append(self._resolve_one(skill_id))
        return resolved

    def manifest(self, skill_ids: list[str]) -> list[dict[str, Any]]:
        return [entry.model_dump(mode="json") for entry, _ in self.resolve(skill_ids)]

    def skill_prompt_entries(
        self, skill_ids: list[str], *, max_chars: int = MAX_SKILL_PROMPT_CHARS
    ) -> list[dict[str, Any]]:
        """Return enabled skills with their SKILL.md content for prompt injection."""

        entries: list[dict[str, Any]] = []
        for skill_id in skill_ids:
            entry, source = self._resolve_one(skill_id)
            content = (source / "SKILL.md").read_text(encoding="utf-8", errors="replace")
            truncated = len(content) > max_chars
            if truncated:
                content = content[:max_chars] + "\n…（内容过长已截断）"
            entries.append(
                {
                    **entry.model_dump(mode="json"),
                    "content": content,
                    "truncated": truncated,
                }
            )
        return entries

    def read_only_mounts(self, skill_ids: list[str]) -> list[tuple[str, str]]:
        return [(str(source), entry.mount_path) for entry, source in self.resolve(skill_ids)]

    def list_skills(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        entries: list[dict[str, Any]] = []
        for child in sorted(self.root.iterdir()):
            if not child.is_dir():
                continue
            if not (child / "SKILL.md").is_file():
                continue
            entries.append(self._resolve_one(child.name)[0].model_dump(mode="json"))
        return entries

    def describe_skill(self, skill_id: str) -> dict[str, Any]:
        entry, source = self._resolve_one(skill_id)
        files = sorted(
            (
                {
                    "path": str(item.relative_to(source)).replace("\\", "/"),
                    "size": item.stat().st_size,
                }
                for item in source.rglob("*")
                if item.is_file()
            ),
            key=lambda item: item["path"],
        )
        return {**entry.model_dump(mode="json"), "files": files}

    def install_skill(self, skill_id: str, files: dict[str, bytes]) -> dict[str, Any]:
        """Install a skill package under the configured root (overwrite = new version)."""

        if not self._valid_id.fullmatch(skill_id):
            raise ValueError(f"invalid skill id: {skill_id}")
        if not files.get("SKILL.md"):
            raise ValueError("skill package must contain SKILL.md")
        staging = self.root / f".{skill_id}.staging-{uuid4().hex[:8]}"
        try:
            staging.mkdir(parents=True, exist_ok=False)
            total = 0
            for name, data in files.items():
                rel = Path(name)
                if rel.is_absolute() or ".." in rel.parts:
                    raise ValueError(f"skill entry escapes the package: {name}")
                if len(data) > MAX_ENTRY_SIZE:
                    raise ValueError(f"skill entry too large: {name}")
                target = staging.joinpath(*rel.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                total += len(data)
                if total > MAX_UNPACKED_SIZE:
                    raise ValueError("skill package exceeds unpacked size limit")
            skill_file = staging / "SKILL.md"
            digest = hashlib.sha256(skill_file.read_bytes()).hexdigest()
            final = self.root / skill_id
            if final.exists():
                shutil.rmtree(final)
            shutil.move(str(staging), str(final))
            return SkillManifestEntry(
                skill_id=skill_id,
                content_hash=digest,
                mount_path=f"/opt/agent-skills/{skill_id}",
            ).model_dump(mode="json")
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def install_zip(self, skill_id: str, payload: bytes) -> dict[str, Any]:
        if len(payload) > MAX_UPLOAD_SIZE:
            raise ValueError("skill package exceeds upload size limit")
        files: dict[str, bytes] = {}
        total = 0
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ENTRY_COUNT:
                raise ValueError("skill package contains too many entries")
            for info in infos:
                if info.is_dir():
                    continue
                rel = Path(info.filename)
                if rel.is_absolute() or ".." in rel.parts:
                    raise ValueError(f"skill entry escapes the package: {info.filename}")
                if info.file_size > MAX_ENTRY_SIZE:
                    raise ValueError(f"skill entry too large: {info.filename}")
                total += info.file_size
                if total > MAX_UNPACKED_SIZE:
                    raise ValueError("skill package exceeds unpacked size limit")
                files[info.filename] = archive.read(info)
        return self.install_skill(skill_id, files)

    def remove_skill(self, skill_id: str) -> bool:
        if not self._valid_id.fullmatch(skill_id):
            raise ValueError(f"invalid skill id: {skill_id}")
        target = (self.root / skill_id).resolve()
        if target.parent != self.root.resolve():
            raise ValueError(f"skill escapes configured root: {skill_id}")
        if not target.is_dir():
            return False
        shutil.rmtree(target)
        return True

    def _resolve_one(self, skill_id: str) -> tuple[SkillManifestEntry, Path]:
        if not self._valid_id.fullmatch(skill_id):
            raise ValueError(f"invalid skill id: {skill_id}")
        source = (self.root / skill_id).resolve()
        if source.parent != self.root:
            raise ValueError(f"skill escapes configured root: {skill_id}")
        skill_file = source / "SKILL.md"
        if not skill_file.is_file():
            raise FileNotFoundError(f"authorized skill is missing SKILL.md: {skill_id}")
        digest = hashlib.sha256(skill_file.read_bytes()).hexdigest()
        return (
            SkillManifestEntry(
                skill_id=skill_id,
                content_hash=digest,
                mount_path=f"/opt/agent-skills/{skill_id}",
            ),
            source,
        )

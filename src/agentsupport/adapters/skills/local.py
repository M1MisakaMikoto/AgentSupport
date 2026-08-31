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


class SkillManifestEntry(BaseModel):
    skill_id: str
    content_hash: str
    mount_path: str


class LocalSkillProvider:
    """Authorized local Skill source with deterministic read-only mount metadata.

    Skills are stored under ``{root}/{skill_id}`` (global namespace) or
    ``{root}/tenants/{tenant_id}/{skill_id}`` (tenant namespace). A ``None``
    tenant keeps the historical global behavior so existing deployments and
    tests are unaffected.
    """

    _valid_id = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    _valid_tenant = re.compile(r"^[^/\\]{1,120}$")

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _namespace(self, tenant_id: str | None) -> Path:
        if tenant_id is None:
            return self.root
        if not self._valid_tenant.fullmatch(tenant_id) or tenant_id in {".", ".."}:
            raise ValueError(f"invalid tenant id: {tenant_id}")
        tenants_root = (self.root / "tenants").resolve()
        namespace = (tenants_root / tenant_id).resolve()
        if namespace.parent != tenants_root:
            raise ValueError(f"tenant escapes configured root: {tenant_id}")
        return namespace

    def resolve(
        self, skill_ids: list[str], *, tenant_id: str | None = None
    ) -> list[tuple[SkillManifestEntry, Path]]:
        resolved: list[tuple[SkillManifestEntry, Path]] = []
        for skill_id in skill_ids:
            resolved.append(self._resolve_one(skill_id, tenant_id=tenant_id))
        return resolved

    def manifest(
        self, skill_ids: list[str], *, tenant_id: str | None = None
    ) -> list[dict[str, Any]]:
        return [
            entry.model_dump(mode="json")
            for entry, _ in self.resolve(skill_ids, tenant_id=tenant_id)
        ]

    def skill_prompt_entries(
        self,
        skill_ids: list[str],
        *,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return enabled skills with their full SKILL.md content for prompt injection."""

        entries: list[dict[str, Any]] = []
        for skill_id in skill_ids:
            entry, source = self._resolve_one(skill_id, tenant_id=tenant_id)
            content = (source / "SKILL.md").read_text(encoding="utf-8", errors="replace")
            entries.append(
                {
                    **entry.model_dump(mode="json"),
                    "content": content,
                }
            )
        return entries

    def read_only_mounts(
        self, skill_ids: list[str], *, tenant_id: str | None = None
    ) -> list[tuple[str, str]]:
        return [
            (str(source), entry.mount_path)
            for entry, source in self.resolve(skill_ids, tenant_id=tenant_id)
        ]

    def list_skills(self, *, tenant_id: str | None = None) -> list[dict[str, Any]]:
        namespace = self._namespace(tenant_id)
        if not namespace.is_dir():
            return []
        entries: list[dict[str, Any]] = []
        for child in sorted(namespace.iterdir()):
            if not child.is_dir():
                continue
            if not (child / "SKILL.md").is_file():
                continue
            entries.append(self._resolve_one(child.name, tenant_id=tenant_id)[0].model_dump(mode="json"))
        return entries

    def describe_skill(
        self, skill_id: str, *, tenant_id: str | None = None
    ) -> dict[str, Any]:
        entry, source = self._resolve_one(skill_id, tenant_id=tenant_id)
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

    def install_skill(
        self,
        skill_id: str,
        files: dict[str, bytes],
        *,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Install a skill package under the configured namespace (overwrite = new version)."""

        if not self._valid_id.fullmatch(skill_id):
            raise ValueError(f"invalid skill id: {skill_id}")
        if not files.get("SKILL.md"):
            raise ValueError("skill package must contain SKILL.md")
        namespace = self._namespace(tenant_id)
        staging = namespace / f".{skill_id}.staging-{uuid4().hex[:8]}"
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
            final = namespace / skill_id
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

    def install_zip(
        self, skill_id: str, payload: bytes, *, tenant_id: str | None = None
    ) -> dict[str, Any]:
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
        return self.install_skill(skill_id, files, tenant_id=tenant_id)

    def remove_skill(self, skill_id: str, *, tenant_id: str | None = None) -> bool:
        if not self._valid_id.fullmatch(skill_id):
            raise ValueError(f"invalid skill id: {skill_id}")
        namespace = self._namespace(tenant_id)
        target = (namespace / skill_id).resolve()
        if target.parent != namespace:
            raise ValueError(f"skill escapes configured root: {skill_id}")
        if not target.is_dir():
            return False
        shutil.rmtree(target)
        return True

    def _resolve_one(
        self, skill_id: str, *, tenant_id: str | None = None
    ) -> tuple[SkillManifestEntry, Path]:
        if not self._valid_id.fullmatch(skill_id):
            raise ValueError(f"invalid skill id: {skill_id}")
        namespace = self._namespace(tenant_id)
        source = (namespace / skill_id).resolve()
        if source.parent != namespace:
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

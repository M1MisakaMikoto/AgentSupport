from __future__ import annotations

import base64
import hashlib
import logging
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

#: Skill bodies are never truncated; a large one is only warned about because
#: the agent is told to locate the part it needs with grep before reading.
SKILL_MD_WARN_BYTES = 64 * 1024

logger = logging.getLogger(__name__)

_FRONTMATTER = re.compile(r"\A---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|\Z)", re.DOTALL)


def parse_skill_frontmatter(content: str) -> dict[str, str]:
    """Parse the YAML-ish frontmatter block of a SKILL.md (flat string fields)."""

    match = _FRONTMATTER.match(content or "")
    if not match:
        return {}
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator:
            continue
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields


def skill_catalog_entry(skill_id: str, content: str) -> dict[str, Any]:
    """The only thing a catalog exposes: id, name and description."""

    frontmatter = parse_skill_frontmatter(content)
    return {
        "skill_id": skill_id,
        "name": frontmatter.get("name", ""),
        "description": frontmatter.get("description", ""),
    }


class SkillManifestEntry(BaseModel):
    skill_id: str
    content_hash: str


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

    def skill_catalog(
        self, skill_ids: list[str], *, tenant_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return the catalog (name + description) for the candidate pool."""

        entries: list[dict[str, Any]] = []
        for skill_id in skill_ids:
            _, source = self._resolve_one(skill_id, tenant_id=tenant_id)
            content = (source / "SKILL.md").read_text(encoding="utf-8", errors="replace")
            entries.append(skill_catalog_entry(skill_id, content))
        return entries

    def skill_package(
        self, skill_ids: list[str], *, tenant_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return whole skill directories (base64) for materialization in the runner."""

        packages: list[dict[str, Any]] = []
        for skill_id in skill_ids:
            _, source = self._resolve_one(skill_id, tenant_id=tenant_id)
            files: list[dict[str, str]] = []
            for item in sorted(source.rglob("*")):
                if not item.is_file():
                    continue
                relative = str(item.relative_to(source)).replace("\\", "/")
                files.append(
                    {
                        "path": relative,
                        "content_b64": base64.b64encode(item.read_bytes()).decode("ascii"),
                    }
                )
            packages.append({"skill_id": skill_id, "files": files})
        return packages

    def list_skills(self, *, tenant_id: str | None = None) -> list[dict[str, Any]]:
        """List the skills a namespace can use: its own first, then the shared ones."""

        found: dict[str, dict[str, Any]] = {}
        for namespace in reversed(self._search_namespaces(tenant_id)):
            if not namespace.is_dir():
                continue
            for child in sorted(namespace.iterdir()):
                if not child.is_dir() or not (child / "SKILL.md").is_file():
                    continue
                digest = hashlib.sha256(
                    (child / "SKILL.md").read_bytes()
                ).hexdigest()
                found[child.name] = SkillManifestEntry(
                    skill_id=child.name, content_hash=digest
                ).model_dump(mode="json")
        return [found[key] for key in sorted(found)]

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
            skill_text = skill_file.read_text(encoding="utf-8", errors="replace")
            frontmatter = parse_skill_frontmatter(skill_text)
            if not frontmatter.get("name") or not frontmatter.get("description"):
                raise ValueError(
                    "skill package must declare frontmatter name and description"
                )
            if skill_file.stat().st_size > SKILL_MD_WARN_BYTES:
                logger.warning(
                    "skill %s has a large SKILL.md (%d bytes); the agent is told to grep "
                    "before reading it in full",
                    skill_id,
                    skill_file.stat().st_size,
                )
            digest = hashlib.sha256(skill_file.read_bytes()).hexdigest()
            final = namespace / skill_id
            if final.exists():
                shutil.rmtree(final)
            shutil.move(str(staging), str(final))
            return SkillManifestEntry(
                skill_id=skill_id,
                content_hash=digest,
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
        for namespace in self._search_namespaces(tenant_id):
            source = (namespace / skill_id).resolve()
            if source.parent != namespace:
                raise ValueError(f"skill escapes configured root: {skill_id}")
            skill_file = source / "SKILL.md"
            if not skill_file.is_file():
                continue
            digest = hashlib.sha256(skill_file.read_bytes()).hexdigest()
            return (
                SkillManifestEntry(skill_id=skill_id, content_hash=digest),
                source,
            )
        raise FileNotFoundError(f"authorized skill is missing SKILL.md: {skill_id}")

    def _search_namespaces(self, tenant_id: str | None) -> list[Path]:
        """Tenant namespace first, then the shared/global one.

        A tenant never sees another tenant's directory: the fallback is only ever
        the root-level shared namespace.
        """

        namespace = self._namespace(tenant_id)
        if namespace == self.root:
            return [self.root]
        return [namespace, self.root]

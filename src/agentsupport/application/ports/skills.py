from __future__ import annotations

from typing import Any, Protocol


class SkillProvider(Protocol):

    def skill_catalog(
        self,
        skill_ids: list[str],
        *,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def skill_package(
        self,
        skill_ids: list[str],
        *,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def list_skills(self, *, tenant_id: str | None = None) -> list[dict[str, Any]]: ...

    def describe_skill(
        self, skill_id: str, *, tenant_id: str | None = None
    ) -> dict[str, Any]: ...

    def install_skill(
        self,
        skill_id: str,
        files: dict[str, bytes],
        *,
        tenant_id: str | None = None,
    ) -> dict[str, Any]: ...

    def install_zip(
        self, skill_id: str, payload: bytes, *, tenant_id: str | None = None
    ) -> dict[str, Any]: ...

    def remove_skill(self, skill_id: str, *, tenant_id: str | None = None) -> bool: ...

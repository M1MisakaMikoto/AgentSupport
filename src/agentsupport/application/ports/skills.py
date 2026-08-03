from __future__ import annotations

from typing import Any, Protocol


class SkillProvider(Protocol):
    def manifest(self, skill_ids: list[str]) -> list[dict[str, Any]]: ...

    def read_only_mounts(self, skill_ids: list[str]) -> list[tuple[str, str]]: ...

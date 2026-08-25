"""Shared helpers for the AgentSupport x Temporal lab experiments.

The lab proves that every capability of the current checkpoint mechanism
(pause/resume, cancel, idempotent commands, lease/heartbeat expiry, crash
recovery, and the Trae-session reconstruction bundle) is either natively
provided by Temporal or combinable with it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from temporalio.client import Client

DEFAULT_ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
NAMESPACE = "default"
TASK_QUEUE = "agentsupport-lab"


def address() -> str:
    return DEFAULT_ADDRESS


async def connect() -> Client:
    return await Client.connect(address(), namespace=NAMESPACE)


class SideEffectStore:
    """Append-only on-disk log proving which activities actually executed.

    This is the experiment's source of truth for "did this side effect run,
    and how many times" -- used to prove Temporal does NOT re-execute
    completed activities after a crash and DOES deduplicate idempotent
    signals.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, name: str, detail: str = "") -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = self._read()
        count = sum(1 for item in data if item["name"] == name) + 1
        data.append({"name": name, "detail": detail, "count": count})
        self._write(data)
        return count

    def count(self, name: str) -> int:
        return sum(1 for item in self._read() if item["name"] == name)

    def details(self, name: str) -> list[str]:
        return [str(item["detail"]) for item in self._read() if item["name"] == name]

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: list[dict]) -> None:
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def fresh_store(path: str | Path) -> SideEffectStore:
    """Return a SideEffectStore with any prior run's evidence removed."""

    p = Path(path)
    if p.exists():
        p.unlink()
    return SideEffectStore(p)

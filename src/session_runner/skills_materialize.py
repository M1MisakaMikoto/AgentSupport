"""Materialize one run's skill package into a read-only directory.

The control plane ships whole skill directories with the run request; the
runner writes them under ``<skills_root>/<run_id>/`` so the agent can read them
with the gated ``bash`` channel. The directory is made read-only after writing:
that guarantee is enforced by the filesystem, not by the command whitelist, so
it holds in every conversation mode.
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Ceiling for one run's materialized skills (base64 payload decoded size).
MAX_PACKAGE_BYTES = 32 * 1024 * 1024

_READ_ONLY_DIR = 0o555
_READ_ONLY_FILE = 0o444


def materialize_skill_package(
    packages: list[dict[str, Any]], root: Path
) -> Path | None:
    """Write ``packages`` under ``root`` (read-only).

    Returns ``None`` when there is nothing to materialize — an empty candidate
    pool must not create a directory.
    """

    root = Path(root)
    if not packages:
        cleanup_skill_package(root)
        return None
    if root.exists():
        _remove_tree(root)
    total = 0
    for package in packages or []:
        skill_id = _safe_segment(str(package.get("skill_id") or ""), "skill id")
        skill_root = root / skill_id
        skill_root.mkdir(parents=True, exist_ok=True)
        for entry in package.get("files") or []:
            relative = _safe_relative_path(str(entry.get("path") or ""))
            payload = base64.b64decode(str(entry.get("content_b64") or ""))
            total += len(payload)
            if total > MAX_PACKAGE_BYTES:
                raise ValueError("skill package exceeds the materialization size limit")
            target = skill_root.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
    _make_read_only(root)
    return root


def cleanup_skill_package(root: Path) -> None:
    """Remove a materialized package directory (best effort, never raises)."""

    try:
        if Path(root).exists():
            _remove_tree(Path(root))
    except OSError as exc:  # pragma: no cover - filesystem dependent
        logger.warning("failed to clean up materialized skills at %s: %s", root, exc)


def _safe_segment(value: str, label: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value or ":" in value:
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def _safe_relative_path(value: str) -> Path:
    """Reject absolute, drive-anchored and parent-escaping entries.

    Checked as text on purpose: ``Path('/etc/passwd').is_absolute()`` is False on
    Windows, where joining it would silently escape to the drive root.
    """

    normalized = value.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if (
        not parts
        or normalized.startswith("/")
        or ".." in parts
        or any(":" in part for part in parts)
    ):
        raise ValueError(f"skill entry escapes the package: {value!r}")
    return Path(*parts)


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        os.chmod(path, _READ_ONLY_FILE if path.is_file() else _READ_ONLY_DIR)
    os.chmod(root, _READ_ONLY_DIR)


def _remove_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        try:
            os.chmod(path, 0o755 if path.is_dir() else 0o644)
        except OSError:  # pragma: no cover - best effort
            pass
    try:
        os.chmod(root, 0o755)
    except OSError:  # pragma: no cover - best effort
        pass
    shutil.rmtree(root, ignore_errors=True)

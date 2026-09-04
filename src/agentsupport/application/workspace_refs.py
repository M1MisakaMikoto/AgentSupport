"""Map a control-plane workspace directory to the path a runner can see."""

from __future__ import annotations

from pathlib import Path, PurePosixPath


def build_workspace_ref(
    core_runner_workspace_root: str | None,
    workspace_root_path: str,
) -> str:
    """Return the runner-visible reference for one workspace directory.

    When the runner shares the control-plane workspace root under a stable
    mount (``core_runner_workspace_root``, e.g. compose's ``/workspace-data``),
    the reference is the posix-joined per-workspace path under that mount.
    Otherwise the runner runs on the same filesystem and receives the real
    directory path, so tool path validation limits it to its own session
    workspace.
    """
    if core_runner_workspace_root:
        base = PurePosixPath(
            "/" + str(core_runner_workspace_root).replace("\\", "/").lstrip("/")
        )
        return str(base / Path(workspace_root_path).name)
    return workspace_root_path

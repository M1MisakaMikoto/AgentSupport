"""Unit tests for runner-visible workspace references."""

from __future__ import annotations

from agentsupport.application.workspace_refs import build_workspace_ref


def test_shared_mount_returns_posix_per_workspace_path() -> None:
    ref = build_workspace_ref("/workspace-data", "/workspace-data/abc-123")
    assert ref == "/workspace-data/abc-123"


def test_mount_with_windows_root_keeps_directory_name() -> None:
    ref = build_workspace_ref(r"D:\dev\ws", r"D:\dev\ws\abc-123")
    assert ref == "/D:/dev/ws/abc-123"


def test_no_mount_returns_real_path() -> None:
    ref = build_workspace_ref(None, r"D:\dev\ws\abc-123")
    assert ref == r"D:\dev\ws\abc-123"

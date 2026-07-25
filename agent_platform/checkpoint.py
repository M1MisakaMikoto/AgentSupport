from __future__ import annotations

import hashlib
import json
from typing import Any

from .domain import Checkpoint


def context_bundle_hash(checkpoint: Checkpoint) -> str:
    encoded = json.dumps(
        checkpoint.context_bundle.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def tool_policy_hash(policy: dict[str, Any]) -> str:
    payload = {
        "allowed_tools": sorted(str(item) for item in policy.get("allowed_tools", [])),
        "approval_required_tools": sorted(
            str(item) for item in policy.get("approval_required_tools", [])
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def tool_versions_hash(tools: list[dict[str, Any]]) -> str:
    payload = sorted(
        (
            {"name": str(item.get("name", "")), "version": str(item.get("version", ""))}
            for item in tools
        ),
        key=lambda item: (item["name"], item["version"]),
    )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_checkpoint(
    checkpoint: Checkpoint,
    *,
    lease_epoch: int,
    expected_tool_batch_hash: str | None = None,
    expected_tool_policy_hash: str | None = None,
    expected_tool_versions_hash: str | None = None,
) -> None:
    if checkpoint.workspace_write_lease_epoch != lease_epoch:
        raise ValueError("workspace write lease epoch mismatch")
    if context_bundle_hash(checkpoint) != checkpoint.context_bundle_hash:
        raise ValueError("context bundle hash mismatch")
    if (
        expected_tool_batch_hash is not None
        and checkpoint.tool_batch_hash != expected_tool_batch_hash
    ):
        raise ValueError("tool batch hash mismatch")
    if (
        expected_tool_policy_hash is not None
        and checkpoint.tool_policy_hash != expected_tool_policy_hash
    ):
        raise ValueError("tool policy hash mismatch")
    if (
        expected_tool_versions_hash is not None
        and checkpoint.tool_versions_hash != expected_tool_versions_hash
    ):
        raise ValueError("tool versions hash mismatch")

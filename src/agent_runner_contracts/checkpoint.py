from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

#: Bound for ``context_bundle.recent_events`` in persisted checkpoints. The
#: full event history is already stored in ``conversation_events``; duplicating
#: every event into every checkpoint makes checkpoint rows grow linearly with
#: the conversation length. Checkpoints only need the recent window for
#: resume-time context reconstruction.
RECENT_EVENTS_LIMIT = 200


class ContextBundle(BaseModel):
    task: str
    conversation_id: UUID
    workspace_ref: str
    recent_events: list[dict[str, Any]] = Field(default_factory=list)
    skill_manifest: list[dict[str, Any]] = Field(default_factory=list)
    skills: list[dict[str, Any]] = Field(default_factory=list)
    tool_policy: dict[str, Any] = Field(default_factory=dict)
    mcp_refs: list[Any] = Field(default_factory=list)


class Checkpoint(BaseModel):
    checkpoint_id: UUID = Field(default_factory=uuid4)
    conversation_id: UUID
    run_id: UUID
    last_event_seq: int
    context_bundle: ContextBundle
    pending_interaction: dict[str, Any] | None = None
    pending_tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_batch_hash: str | None = None
    tool_policy_hash: str | None = None
    tool_versions_hash: str | None = None
    workspace_ref: str
    workspace_write_lease_epoch: int
    core_type: str = "fake"
    core_version: str = "0.1.0"
    context_bundle_hash: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


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
    if expected_tool_batch_hash is not None and checkpoint.tool_batch_hash != expected_tool_batch_hash:
        raise ValueError("tool batch hash mismatch")
    if expected_tool_policy_hash is not None and checkpoint.tool_policy_hash != expected_tool_policy_hash:
        raise ValueError("tool policy hash mismatch")
    if expected_tool_versions_hash is not None and checkpoint.tool_versions_hash != expected_tool_versions_hash:
        raise ValueError("tool versions hash mismatch")

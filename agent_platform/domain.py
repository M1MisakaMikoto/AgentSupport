from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class ExecutionState(StrEnum):
    QUEUED = "QUEUED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    WAITING_INPUT = "WAITING_INPUT"
    SUSPENDING = "SUSPENDING"
    PAUSED = "PAUSED"
    RESUMING = "RESUMING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    LOST = "LOST"


TERMINAL_STATES = {
    ExecutionState.COMPLETED,
    ExecutionState.FAILED,
    ExecutionState.CANCELLED,
    ExecutionState.LOST,
}


class Workspace(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    name: str
    root_path: str
    created_at: datetime = Field(default_factory=utc_now)


class Session(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    lease_epoch: int = 0
    active_container_id: str | None = None
    active_run_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)


class RunProjection(BaseModel):
    run_id: UUID = Field(default_factory=uuid4)
    state: ExecutionState = ExecutionState.QUEUED
    last_seq: int = 0
    pending_interaction: dict[str, Any] | None = None
    result_summary: dict[str, Any] | None = None
    checkpoint_id: UUID | None = None


class Conversation(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    parent_conversation_id: UUID | None = None
    task: str
    created_at: datetime = Field(default_factory=utc_now)
    run: RunProjection = Field(default_factory=RunProjection)


class ContextBundle(BaseModel):
    task: str
    conversation_id: UUID
    workspace_ref: str
    recent_events: list[dict[str, Any]] = Field(default_factory=list)
    skill_manifest: list[dict[str, Any]] = Field(default_factory=list)
    tool_policy: dict[str, Any] = Field(default_factory=dict)
    mcp_refs: list[str] = Field(default_factory=list)


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
    created_at: datetime = Field(default_factory=utc_now)

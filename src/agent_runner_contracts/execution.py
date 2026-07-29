from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class RunRequest(BaseModel):
    run_id: UUID
    conversation_id: UUID
    session_id: UUID
    container_id: str
    lease_epoch: int
    fence_epoch: int = 0
    correlation_id: str
    context_bundle: dict[str, Any]
    workspace_ref: str = "/workspace"
    tool_policy: dict[str, Any] = Field(default_factory=dict)
    core_version: str = "0.1.0"


class InputRequest(BaseModel):
    interaction_id: str
    value: Any
    command_id: UUID | None = None


class ApprovalRequest(BaseModel):
    approval_id: str
    decision: str
    command_id: UUID | None = None


class CommandRequest(BaseModel):
    command_id: UUID | None = None


class CheckpointRequest(BaseModel):
    reason: str


class ResumeRequest(BaseModel):
    checkpoint: dict[str, Any]
    value: Any = None
    command_id: UUID | None = None
    session_id: UUID | None = None
    container_id: str | None = None
    lease_epoch: int | None = None
    fence_epoch: int | None = None
    correlation_id: str | None = None

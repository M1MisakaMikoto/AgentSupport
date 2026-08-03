from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class JobState(StrEnum):
    READY = "READY"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    PAUSED = "PAUSED"
    RETRY = "RETRY"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class CommandState(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    APPLIED = "APPLIED"
    FAILED = "FAILED"


class ExecutionJob(BaseModel):
    id: UUID
    run_id: UUID
    conversation_id: UUID
    session_id: UUID
    workspace_id: UUID
    state: JobState
    priority: int = 0
    available_at: datetime
    claimed_by: str | None = None
    claim_token: int = 0
    lease_expires_at: datetime | None = None
    attempts: int = 0
    max_attempts: int = 3
    last_error: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    state_changed_at: datetime


class JobClaim(BaseModel):
    job: ExecutionJob
    previous_state: JobState
    claim_token: int
    lease_expires_at: datetime
    slot_no: int
    fence_epoch: int


class RunCommand(BaseModel):
    id: UUID
    run_id: UUID
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str
    state: CommandState
    claimed_by: str | None = None
    attempts: int = 0
    created_at: datetime


class RunnerEndpoint(BaseModel):
    run_id: UUID
    session_id: UUID
    runtime_id: str
    endpoint: str
    lease_epoch: int
    owner_instance_id: str
    status: str
    last_runner_seq: int = 0
    heartbeat_at: datetime


class OutboxNotification(BaseModel):
    id: UUID
    topic: str
    aggregate_id: UUID
    payload: dict[str, Any]
    attempts: int
    created_at: datetime

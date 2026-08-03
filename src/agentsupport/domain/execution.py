from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from agent_runner_contracts.checkpoint import Checkpoint, ContextBundle


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


class RunProjection(BaseModel):
    run_id: UUID = Field(default_factory=uuid4)
    state: ExecutionState = ExecutionState.QUEUED
    last_seq: int = 0
    pending_interaction: dict[str, Any] | None = None
    result_summary: dict[str, Any] | None = None
    checkpoint_id: UUID | None = None


__all__ = [
    "TERMINAL_STATES",
    "Checkpoint",
    "ContextBundle",
    "ExecutionState",
    "RunProjection",
    "utc_now",
]

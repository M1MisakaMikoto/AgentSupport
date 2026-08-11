"""Wire models for Runner self-registration between AgentSupport and Session Runner."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

#: Capabilities a Runner can advertise. The control plane routes runs only to
#: registrations that cover every required capability.
RUNNER_CAPABILITIES = ("run", "input", "checkpoint", "cancel", "events", "resume")


class RunnerRegistrationRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=64)
    endpoint: str = Field(min_length=1, max_length=2048)
    version: str = "0.1.0"
    capabilities: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunnerRegistrationResponse(BaseModel):
    runner_id: UUID
    token: str


class RunnerHeartbeat(BaseModel):
    status: str = "READY"
    load: int = 0
    capabilities: list[str] | None = None


class RunnerRegistration(BaseModel):
    runner_id: UUID = Field(default_factory=uuid4)
    provider: str
    endpoint: str
    version: str = "0.1.0"
    capabilities: list[str] = Field(default_factory=list)
    status: str = "READY"
    load: int = 0
    last_heartbeat_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "RUNNER_CAPABILITIES",
    "RunnerHeartbeat",
    "RunnerRegistration",
    "RunnerRegistrationRequest",
    "RunnerRegistrationResponse",
]

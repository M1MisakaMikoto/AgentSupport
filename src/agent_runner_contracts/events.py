from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class EventEnvelope(BaseModel):
    schema_version: str = "1"
    event_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    seq: int
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    source: str = "platform"
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

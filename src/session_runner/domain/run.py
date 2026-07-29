from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from agent_runner_contracts.events import EventEnvelope
from agent_runner_contracts.execution import RunRequest
from agent_runner_contracts.tools import ToolBatch


class RunState:
    """Mutable state owned by one runner process for a single active run."""

    def __init__(self, request: RunRequest) -> None:
        self.request = request
        self.events: list[EventEnvelope] = []
        self.status = "STARTING"
        self.pending_interaction: dict[str, Any] | None = None
        self.tool_executor: Any = None
        self.tool_batch: ToolBatch | None = None
        self.tool_authorization: Any = None
        self.trae_execution: Any = None
        self.background: asyncio.Task[None] | None = None
        self.status_changed = asyncio.Event()
        self.command_results: dict[str, dict[str, Any]] = {}

    def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> EventEnvelope:
        event = EventEnvelope(
            run_id=self.request.run_id,
            seq=len(self.events) + 1,
            type=event_type,
            payload=payload or {},
            source="session_runner",
        )
        self.events.append(event)
        return event

    def cached_command(self, command_id: UUID | None) -> dict[str, Any] | None:
        return self.command_results.get(str(command_id)) if command_id else None

    def remember_command(self, command_id: UUID | None, result: dict[str, Any]) -> None:
        if command_id:
            self.command_results[str(command_id)] = result

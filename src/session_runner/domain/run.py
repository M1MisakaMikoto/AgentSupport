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
        self.mcp_provider: Any = None
        self.background: asyncio.Task[None] | None = None
        self.status_changed = asyncio.Event()
        #: 每 emit 一条事件就置位，唤醒正在订阅 `/runs/{id}/events/stream` 的推送协程。
        self.events_wake = asyncio.Event()
        self.command_results: dict[str, dict[str, Any]] = {}
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:                    # 非事件循环里构造（测试/工具）：退化为不唤醒
            self._loop = None

    def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> EventEnvelope:
        event = EventEnvelope(
            run_id=self.request.run_id,
            seq=len(self.events) + 1,
            type=event_type,
            payload=payload or {},
            source="session_runner",
        )
        self.events.append(event)
        # emit 可能来自模型流的工作线程（`on_text_delta`），唤醒必须回到事件循环上做。
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self.events_wake.set)
        return event

    def cached_command(self, command_id: UUID | None) -> dict[str, Any] | None:
        return self.command_results.get(str(command_id)) if command_id else None

    def remember_command(self, command_id: UUID | None, result: dict[str, Any]) -> None:
        if command_id:
            self.command_results[str(command_id)] = result

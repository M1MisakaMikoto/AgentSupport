"""Event query/stream operations and readiness/metrics."""



from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from agent_runner_contracts.events import EventEnvelope

from ..domain import (
    ExecutionState,
)
from .common import (
    ServiceError,
)


class EventOpsMixin:

    def events(self, conversation_id: UUID, after_seq: int = 0) -> list[EventEnvelope]:
        conversation = self._conversation(conversation_id)
        if self.repository:
            raw = self.repository.list_events(conversation_id, after_seq)
        else:
            raw = self.events_store.list(conversation_id, after_seq)
        tenant_id, user_id, project_id = self._session_labels(conversation.session_id)
        return [
            event.model_copy(
                update={
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "project_id": project_id,
                }
            )
            for event in raw
        ]


    async def stream_events(
        self, conversation_id: UUID, after_seq: int = 0
    ) -> AsyncIterator[EventEnvelope]:
        self._conversation(conversation_id)
        if not self.temporal_mode:
            async for event in self.events_store.stream(conversation_id, after_seq):
                yield event
            return
        cursor = after_seq
        while True:
            events = self.events(conversation_id, cursor)
            if events:
                for event in events:
                    cursor = event.seq
                    yield event
                continue
            try:
                await self.event_notifier.wait(
                    "conversation.events",
                    conversation_id,
                    self.config.event_poll_interval_seconds,
                )
            except Exception:  # noqa: BLE001 - database polling remains authoritative
                await asyncio.sleep(self.config.event_poll_interval_seconds)


    def session_events(self, session_id: UUID, after_seq: int = 0) -> list[EventEnvelope]:
        session = (
            self.repository.get_session(session_id)
            if self.temporal_mode and self.repository
            else self.sessions.get(session_id)
        )
        if session is None:
            raise ServiceError("SESSION_NOT_FOUND", "session does not exist", 404)
        conversations = (
            self.repository.list_conversations()
            if self.temporal_mode and self.repository
            else list(self.conversations.values())
        )
        events = [
            event
            for conversation in conversations
            if conversation.session_id == session_id
            for event in self.events(conversation.id, after_seq)
        ]
        return sorted(events, key=lambda event: event.occurred_at)


    async def stream_session_events(
        self, session_id: UUID, after_seq: int = 0
    ) -> AsyncIterator[EventEnvelope]:
        self.get_session(session_id)
        cursors: dict[UUID, int] = {}
        while True:
            conversations = (
                self.repository.list_conversations()
                if self.temporal_mode and self.repository
                else list(self.conversations.values())
            )
            for conversation in conversations:
                if conversation.session_id != session_id:
                    continue
                cursor = cursors.get(conversation.id, after_seq)
                for event in self.events(conversation.id, cursor):
                    cursors[conversation.id] = event.seq
                    yield event
            try:
                await self.event_notifier.wait(
                    "session.events",
                    session_id,
                    self.config.event_poll_interval_seconds,
                )
            except Exception:  # noqa: BLE001 - polling remains authoritative
                await asyncio.sleep(self.config.event_poll_interval_seconds)


    def readiness(self) -> dict[str, Any]:
        if self.repository:
            self.repository.health_check()
        return {
            "status": "ready",
            "execution_mode": self.config.execution_mode,
            "persistence_mode": self.config.persistence_mode,
            "instance_id": self.instance_id,
        }


    def metrics(self) -> dict[str, int]:
        return {
            "queue_ready": sum(
                item.run.state == ExecutionState.QUEUED
                for item in self.conversations.values()
            ),
            "queue_oldest_ready_seconds": 0,
            "jobs_claimed": 0,
            "jobs_running": sum(
                item.run.state == ExecutionState.RUNNING
                for item in self.conversations.values()
            ),
            "jobs_waiting": sum(
                item.run.state == ExecutionState.WAITING_INPUT
                for item in self.conversations.values()
            ),
            "jobs_paused": sum(
                item.run.state == ExecutionState.PAUSED
                for item in self.conversations.values()
            ),
            "claims_expired": 0,
            "active_runtimes": sum(
                item.active_container_id is not None for item in self.sessions.values()
            ),
            "runner_starting": 0,
            "runner_reconciliation_needed": 0,
            "workspace_lease_contention": 0,
            "outbox_pending": 0,
            "outbox_publication_lag_seconds": 0,
        }

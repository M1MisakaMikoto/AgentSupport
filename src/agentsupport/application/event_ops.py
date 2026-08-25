"""Event query/stream operations and readiness/metrics."""



from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from agent_runner_contracts.events import EventEnvelope

from ..domain import (
    ExecutionState,
)


class EventOpsMixin:

    def events(
        self,
        conversation_id: UUID,
        after_seq: int = 0,
        limit: int | None = None,
    ) -> list[EventEnvelope]:
        conversation = self._conversation(conversation_id)
        if self.repository:
            raw = self.repository.list_events(conversation_id, after_seq, limit)
        else:
            raw = self.events_store.list(conversation_id, after_seq)
            if limit is not None:
                raw = raw[:limit]
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
        conversation = self._conversation(conversation_id)
        if not self.temporal_mode:
            async for event in self.events_store.stream(conversation_id, after_seq):
                yield event
            return
        labels = dict(
            zip(
                ("tenant_id", "user_id", "project_id"),
                self._session_labels(conversation.session_id),
            )
        )
        cursor = after_seq
        while True:
            events = await asyncio.to_thread(
                self.repository.list_events, conversation_id, cursor
            )
            if events:
                for event in events:
                    cursor = event.seq
                    yield event.model_copy(update=labels)
                continue
            try:
                await self.event_notifier.wait(
                    "conversation.events",
                    conversation_id,
                    self.config.event_poll_interval_seconds,
                )
            except Exception:  # noqa: BLE001 - database polling remains authoritative
                await asyncio.sleep(self.config.event_poll_interval_seconds)


    def session_events(
        self,
        session_id: UUID,
        after_seq: int = 0,
        limit: int | None = None,
    ) -> list[EventEnvelope]:
        session = self.get_session(session_id)
        if self.temporal_mode and self.repository:
            events = self.repository.list_session_events(
                session_id, after_seq, limit
            )
        else:
            conversations = list(self.conversations.values())
            events = [
                event
                for conversation in conversations
                if conversation.session_id == session_id
                for event in self.events_store.list(conversation.id, after_seq)
            ]
            if limit is not None:
                events = events[:limit]
        labels = {
            "tenant_id": session.tenant_id,
            "user_id": session.user_id,
            "project_id": session.project_id,
        }
        events = [event.model_copy(update=labels) for event in events]
        return sorted(events, key=lambda event: event.occurred_at)


    async def stream_session_events(
        self, session_id: UUID, after_seq: int = 0
    ) -> AsyncIterator[EventEnvelope]:
        session = self.get_session(session_id)
        labels = {
            "tenant_id": session.tenant_id,
            "user_id": session.user_id,
            "project_id": session.project_id,
        }
        cursors: dict[UUID, int] = {}
        while True:
            if self.temporal_mode and self.repository:
                conversations = await asyncio.to_thread(
                    self.repository.list_conversations, session_id=session_id
                )
            else:
                conversations = list(self.conversations.values())
            for conversation in conversations:
                if conversation.session_id != session_id:
                    continue
                cursor = cursors.get(conversation.id, after_seq)
                if self.temporal_mode and self.repository:
                    raw_events = await asyncio.to_thread(
                        self.repository.list_events, conversation.id, cursor
                    )
                else:
                    raw_events = self.events_store.list(conversation.id, cursor)
                for event in raw_events:
                    cursors[conversation.id] = event.seq
                    yield event.model_copy(update=labels)
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


    def metrics(self) -> dict[str, Any]:
        outbox = (
            self.repository.outbox_stats()
            if self.repository is not None
            else {"pending": 0, "lag_seconds": 0.0}
        )
        if self.temporal_mode and self.repository is not None:
            counts = self.repository.conversation_state_counts()
            oldest = self.repository.oldest_queued_created_at()
            if oldest is not None:
                queue_oldest_ready_seconds = max(
                    0.0, (datetime.now(UTC) - oldest).total_seconds()
                )
            else:
                queue_oldest_ready_seconds = 0.0
            queue_ready = counts.get(ExecutionState.QUEUED.value, 0)
            jobs_running = counts.get(ExecutionState.RUNNING.value, 0)
            jobs_waiting = counts.get(ExecutionState.WAITING_INPUT.value, 0)
            jobs_paused = counts.get(ExecutionState.PAUSED.value, 0)
            active_runtimes = self.repository.active_container_count()
        else:
            queue_ready = sum(
                item.run.state == ExecutionState.QUEUED
                for item in self.conversations.values()
            )
            queue_oldest_ready_seconds = 0
            jobs_running = sum(
                item.run.state == ExecutionState.RUNNING
                for item in self.conversations.values()
            )
            jobs_waiting = sum(
                item.run.state == ExecutionState.WAITING_INPUT
                for item in self.conversations.values()
            )
            jobs_paused = sum(
                item.run.state == ExecutionState.PAUSED
                for item in self.conversations.values()
            )
            active_runtimes = sum(
                item.active_container_id is not None for item in self.sessions.values()
            )
        return {
            "queue_ready": queue_ready,
            "queue_oldest_ready_seconds": queue_oldest_ready_seconds,
            "jobs_claimed": 0,
            "jobs_running": jobs_running,
            "jobs_waiting": jobs_waiting,
            "jobs_paused": jobs_paused,
            "claims_expired": 0,
            "active_runtimes": active_runtimes,
            "runner_starting": 0,
            "runner_reconciliation_needed": 0,
            "workspace_lease_contention": 0,
            "outbox_pending": outbox["pending"],
            "outbox_publication_lag_seconds": outbox["lag_seconds"],
        }

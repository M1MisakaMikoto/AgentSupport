"""Application use cases for the AgentSupport control plane."""



from __future__ import annotations

import asyncio
import os
import socket
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from agent_runner_contracts.events import EventEnvelope

from ..domain import (
    Checkpoint,
    Conversation,
    McpServer,
    Session,
    Workspace,
)
from .checkpoint_ops import CheckpointOpsMixin
from .common import (
    IdempotencyRecord,
    ServiceError,
    _hash_request,
)
from .conversation_ops import ConversationOpsMixin
from .event_ops import EventOpsMixin
from .interaction_ops import InteractionOpsMixin
from .ports import (
    CoreRuntime,
    EventNotifier,
    EventStore,
    RepositoryConflict,
    RunnerRegistry,
    RuntimeDriver,
    SkillProvider,
    WorkspaceProvider,
)
from .runner_ops import RunnerOpsMixin
from .runner_registry import InMemoryRunnerRegistry
from .session_ops import SessionOpsMixin
from .skill_mcp_ops import SkillMcpOpsMixin
from .workspace_ops import WorkspaceOpsMixin


class AgentSupportService(
    WorkspaceOpsMixin,
    SkillMcpOpsMixin,
    SessionOpsMixin,
    ConversationOpsMixin,
    InteractionOpsMixin,
    RunnerOpsMixin,
    CheckpointOpsMixin,
    EventOpsMixin,
):
    """AgentSupport application service composed from domain operation mixins."""


    def __init__(
        self,
        config: Any,
        *,
        events_store: EventStore,
        event_notifier: EventNotifier,
        workspace_provider: WorkspaceProvider,
        skill_provider: SkillProvider,
        runtime_driver: RuntimeDriver,
        core_runtime: CoreRuntime | None,
        repository: Any | None,
        runner_registry: RunnerRegistry | None = None,
        temporal: Any | None = None,
    ) -> None:
        self.config = config
        self.temporal_mode = config.execution_mode == "temporal"
        self.temporal = temporal
        self.instance_id = config.instance_id or f"{socket.gethostname()}:{os.getpid()}"
        self.workspaces: dict[UUID, Workspace] = {}
        self.sessions: dict[UUID, Session] = {}
        self.conversations: dict[UUID, Conversation] = {}
        self.checkpoints: dict[UUID, Checkpoint] = {}
        self.events_store = events_store
        self.event_notifier = event_notifier
        self.workspace_provider = workspace_provider
        self.skill_provider = skill_provider
        self.enabled_skills = [
            item.strip() for item in config.enabled_skills.split(",") if item.strip()
        ]
        self.runtime_driver = runtime_driver
        self.core_runtime = core_runtime
        self.idempotency: dict[tuple[str, str], IdempotencyRecord] = {}
        self.workspace_leases: dict[UUID, UUID] = {}
        self._health_failures: dict[UUID, int] = {}
        self._scheduler_lock = asyncio.Lock()
        self.repository = repository
        self.runner_registry = runner_registry or InMemoryRunnerRegistry()
        self.mcp_servers: dict[str, McpServer] = {}
        if self.temporal_mode and self.repository is None:
            raise ValueError(
                "temporal execution requires a shared persistence backend"
            )
        if self.repository and not self.temporal_mode:
            self._load_persisted_state()


    def _load_persisted_state(self) -> None:
        assert self.repository is not None
        self.workspaces = {item.id: item for item in self.repository.list_workspaces()}
        self.sessions = {item.id: item for item in self.repository.list_sessions()}
        self.conversations = {item.id: item for item in self.repository.list_conversations()}
        for session in self.sessions.values():
            if session.active_container_id:
                self.workspace_leases[session.workspace_id] = session.id
        for conversation in self.conversations.values():
            for event in self.repository.list_events(conversation.id):
                self.events_store.append(conversation.id, event)


    def _idempotent(self, scope: str, key: str | None, payload: dict[str, Any]) -> UUID | None:
        if not key:
            return None
        record = self.idempotency.get((scope, key))
        request_hash = _hash_request(payload)
        if record:
            if record.request_hash != request_hash:
                raise ServiceError(
                    "IDEMPOTENCY_CONFLICT",
                    "idempotency key was reused with a different request",
                    409,
                )
            return record.resource_id
        return None


    async def _runner_endpoint(self, session: Session) -> str | None:
        if session.active_container_id:
            endpoint_method = getattr(self.runtime_driver, "endpoint", None)
            if endpoint_method is not None:
                endpoint = await endpoint_method(session.active_container_id)
                if endpoint:
                    return endpoint
        return self.config.core_runner_url


    async def _register_core_endpoint(
        self, session: Session, run_id: UUID, endpoint: str | None = None
    ) -> str | None:
        if endpoint is None:
            endpoint = await self._runner_endpoint(session)
        if self.core_runtime is not None:
            register = getattr(self.core_runtime, "register_run_endpoint", None)
            if register is not None:
                register(run_id, endpoint)
        return endpoint


    @staticmethod
    def _tool_policy() -> dict[str, Any]:
        return {
            "allowed_tools": [
                "bash",
                "str_replace_based_edit_tool",
                "json_edit_tool",
                "sequentialthinking",
                "task_done",
            ],
            "approval_required_tools": [
                "bash",
                "str_replace_based_edit_tool",
                "json_edit_tool",
            ],
        }


    # ------------------------------------------------------------------
    # Session configuration resolution (config travels with the session)
    # ------------------------------------------------------------------

    def _skills_for_session(self, session: Session) -> list[str]:
        if session.config and not session.config.is_empty():
            return session.config.enabled_skill_ids()
        return self.enabled_skills


    def _skills_for_conversation(
        self, conversation: Conversation, session: Session
    ) -> list[str]:
        if conversation.skills is not None:
            return [skill.skill_id for skill in conversation.skills if skill.enabled]
        return self._skills_for_session(session)


    def _tool_policy_for_session(self, session: Session) -> dict[str, Any]:
        if session.config and not session.config.is_empty():
            policy = session.config.tool_policy_dict()
            if policy.get("allowed_tools") or policy.get("approval_required_tools"):
                return policy
        return self._tool_policy()


    # ------------------------------------------------------------------
    # Missing-precondition auto-completion (configurable, default ON)
    # ------------------------------------------------------------------

    def _auto_create(self, scope: str) -> bool:
        """Whether missing resources of ``scope`` may be auto-created."""

        if not getattr(self.config, "auto_create_missing", False):
            return False
        scopes = getattr(self.config, "auto_create_scopes_set", None)
        if scopes is None:
            raw = getattr(self.config, "auto_create_scopes", "all")
            scopes = (
                {"all"}
                if str(raw).strip() == "all"
                else {item.strip() for item in str(raw).split(",") if item.strip()}
            )
        return "all" in scopes or scope in scopes


    def _remember(
        self, scope: str, key: str | None, payload: dict[str, Any], resource_id: UUID
    ) -> None:
        if key:
            self.idempotency[(scope, key)] = IdempotencyRecord(_hash_request(payload), resource_id)


    def _remember_persisted(
        self,
        scope: str,
        key: str | None,
        payload: dict[str, Any],
        resource_id: UUID,
        response: dict[str, Any],
    ) -> None:
        self._remember(scope, key, payload, resource_id)
        if self.repository and key:
            try:
                self.repository.remember_idempotent(
                    scope, key, _hash_request(payload), resource_id, response
                )
            except RepositoryConflict as exc:
                raise ServiceError("IDEMPOTENCY_CONFLICT", str(exc), 409) from exc


    def _append(
        self,
        conversation: Conversation,
        event_type: str,
        payload: dict[str, Any],
        source: str = "agentsupport",
    ) -> EventEnvelope:
        seq = conversation.run.last_seq + 1
        tenant_id, user_id, project_id = self._session_labels(conversation.session_id)
        event = EventEnvelope(
            run_id=conversation.run.run_id,
            seq=seq,
            type=event_type,
            payload=payload,
            tenant_id=tenant_id,
            user_id=user_id,
            project_id=project_id,
            source=source,
        )
        if self.repository:
            try:
                self.repository.append_event(conversation, event)
            except RepositoryConflict as exc:
                raise ServiceError("EVENT_CONFLICT", str(exc), 409) from exc
        self.events_store.append(conversation.id, event)
        conversation.run.last_seq = seq
        try:
            asyncio.get_running_loop().create_task(
                self.events_store.publish(conversation.id, event)
            )
        except RuntimeError:
            pass
        return event


    def _session_labels(self, session_id: UUID) -> tuple[str | None, str | None, str | None]:
        session = None if self.temporal_mode else self.sessions.get(session_id)
        if not session and self.repository:
            session = self.repository.get_session(session_id)
        if not session:
            return None, None, None
        return session.tenant_id, session.user_id, session.project_id


    def prune_retained_state(self, now: datetime | None = None) -> dict[str, int]:
        """Drop transient in-memory records that outlived their retention windows.

        Only idempotency records and checkpoints that are no longer referenced
        by any conversation are pruned. Workspaces, sessions, conversations and
        events are authoritative data and are intentionally left untouched by
        this pass; PostgreSQL-side retention is handled by the dedicated
        retention process.
        """

        current = now or datetime.now(UTC)
        idempotency_cutoff = current - timedelta(
            hours=self.config.retention_idempotency_hours
        )
        stale_idempotency = [
            key
            for key, record in self.idempotency.items()
            if record.created_at < idempotency_cutoff
        ]
        for key in stale_idempotency:
            del self.idempotency[key]
        referenced = {
            conversation.run.checkpoint_id
            for conversation in self.conversations.values()
            if conversation.run.checkpoint_id is not None
        }
        checkpoint_cutoff = current - timedelta(
            hours=self.config.retention_unreferenced_checkpoints_hours
        )
        stale_checkpoints = [
            checkpoint_id
            for checkpoint_id, checkpoint in self.checkpoints.items()
            if checkpoint_id not in referenced and checkpoint.created_at < checkpoint_cutoff
        ]
        for checkpoint_id in stale_checkpoints:
            del self.checkpoints[checkpoint_id]
        return {
            "idempotency_records": len(stale_idempotency),
            "unreferenced_checkpoints": len(stale_checkpoints),
        }

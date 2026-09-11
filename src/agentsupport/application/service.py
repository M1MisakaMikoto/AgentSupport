"""Application use cases for the AgentSupport control plane."""



from __future__ import annotations

import asyncio
import logging
import os
import socket
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from agent_runner_contracts.events import EventEnvelope

from ..domain import (
    Checkpoint,
    Conversation,
    ConversationMode,
    McpServer,
    Session,
    SkillDraft,
    SkillGenerationRequest,
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
    SkillProvider,
    WorkspaceProvider,
)
from .runner_ops import RunnerOpsMixin
from .runner_registry import InMemoryRunnerRegistry
from .session_ops import SessionOpsMixin
from .skill_generation_ops import (
    ASK_USER_TOOL,
    SkillGenerationOpsMixin,
    is_skill_generation_task,
)
from .skill_mcp_ops import SkillMcpOpsMixin
from .workspace_ops import WorkspaceOpsMixin

logger = logging.getLogger(__name__)

#: 静默模式白名单：工作区文件/文档工具，外加受命令门禁约束的 bash（读取 skill 目录的通道）。
SILENT_MODE_TOOLS = (
    "bash",
    "str_replace_based_edit_tool",
    "json_edit_tool",
    "word_edit_tool",
    "excel_edit_tool",
    "pdf_tool",
    "document_convert_tool",
    "task_done",
)

#: Soft ceiling for in-flight in-process event notifications. A slow notifier
#: (e.g. a wedged Redis connection) would otherwise accumulate one task per
#: event with no visibility; we only warn, never drop notifications.
MAX_PUBLISH_TASKS = 200


class AgentSupportService(
    WorkspaceOpsMixin,
    SkillMcpOpsMixin,
    SessionOpsMixin,
    ConversationOpsMixin,
    InteractionOpsMixin,
    RunnerOpsMixin,
    CheckpointOpsMixin,
    EventOpsMixin,
    SkillGenerationOpsMixin,
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
        self.skill_generations: dict[UUID, SkillGenerationRequest] = {}
        self.skill_drafts: dict[UUID, SkillDraft] = {}
        self.events_store = events_store
        self.event_notifier = event_notifier
        self.workspace_provider = workspace_provider
        self.skill_provider = skill_provider
        self.core_runtime = core_runtime
        self.idempotency: dict[tuple[str, str], IdempotencyRecord] = {}
        self.workspace_leases: dict[UUID, UUID] = {}
        self._health_failures: dict[UUID, int] = {}
        self._scheduler_lock = asyncio.Lock()
        self._publish_tasks: set[asyncio.Task[Any]] = set()
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
        del session
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

    async def _cancel_runner_best_effort(self, run_id: UUID) -> None:
        """Best-effort cancel so a timed-out/failed control-plane call does not
        leave the runner executing in the background (orphan run)."""

        if self.core_runtime is None:
            return
        try:
            await self.core_runtime.cancel(run_id)
        except Exception as exc:  # noqa: BLE001 - cancel is best effort
            logger.warning(
                "best-effort runner cancel failed for run %s: %s", run_id, exc
            )

    def _schedule_publish(self, conversation_id: UUID, event: EventEnvelope) -> None:
        """Schedule the in-process event notification with tracking and error
        logging (replaces untracked fire-and-forget tasks)."""

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if len(self._publish_tasks) >= MAX_PUBLISH_TASKS:
            logger.warning(
                "event publish backlog reached %d tasks; notifier may be slow",
                MAX_PUBLISH_TASKS,
            )
        task = loop.create_task(self.events_store.publish(conversation_id, event))
        self._publish_tasks.add(task)
        task.add_done_callback(self._on_publish_done)

    def _on_publish_done(self, task: asyncio.Task[Any]) -> None:
        self._publish_tasks.discard(task)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except (asyncio.CancelledError, RuntimeError):
            return
        if exc is not None:
            logger.error(
                "event publish task failed (%s): %s", task.get_name(), exc,
                exc_info=exc,
            )


    @staticmethod
    def _tool_policy() -> dict[str, Any]:
        return {
            "allowed_tools": [
                "bash",
                "str_replace_based_edit_tool",
                "json_edit_tool",
                "word_edit_tool",
                "excel_edit_tool",
                "pdf_tool",
                "document_convert_tool",
                "sequentialthinking",
                "task_done",
            ],
            "approval_required_tools": [
                "bash",
                "str_replace_based_edit_tool",
                "json_edit_tool",
                "word_edit_tool",
                "excel_edit_tool",
                "pdf_tool",
                "document_convert_tool",
            ],
        }


    # ------------------------------------------------------------------
    # Session configuration resolution (config travels with the session)
    # ------------------------------------------------------------------

    def _session_recent_events(
        self,
        session: Session,
        conversation: Conversation,
    ) -> list[dict[str, Any]]:
        """Aggregate the session-wide conversation history for a run request.

        A conversation is one exchange; a session is the collection of many
        exchanges. Agents therefore see the tool-call history inside the
        current conversation plus the dialogue history of earlier
        conversations, mirroring how chat/agent products carry context.
        """
        if self.temporal_mode and self.repository:
            conversations = self.repository.list_conversations(session_id=session.id)
            events = self.repository.list_session_events(session.id)
        else:
            conversations = [
                conv for conv in self.conversations.values() if conv.session_id == session.id
            ]
            events = sorted(
                (
                    event
                    for conv in conversations
                    for event in self.events_store.list(conv.id)
                ),
                key=lambda event: event.occurred_at,
            )
        silent_run_ids = {
            str(conv.run.run_id)
            for conv in conversations
            if getattr(conv, "mode", None) == ConversationMode.SILENT
            and conv.run is not None
            and conv.run.run_id is not None
        }
        # Earlier rounds' instructions live on the conversation rows, not the
        # event stream; inject them as user messages so the agent sees the
        # full dialogue history of the session.
        prior = sorted(
            (
                conv
                for conv in conversations
                if conv.id != conversation.id
                and getattr(conv, "mode", None) != ConversationMode.SILENT
            ),
            key=lambda conv: conv.created_at,
        )
        injected = [
            {
                "type": "message",
                "payload": {"content": conv.task, "role": "user"},
                "source": "agentsupport",
                "conversation_id": str(conv.id),
                "occurred_at": conv.created_at.isoformat(),
            }
            for conv in prior
        ]
        merged = sorted(
            injected
            + [
                event.model_dump(mode="json")
                for event in events
                if not silent_run_ids or str(event.run_id) not in silent_run_ids
            ],
            key=lambda event: event.get("occurred_at") or "",
        )
        return merged


    def _skills_for_session(self, session: Session) -> list[str]:
        """The candidate pool: what the session/project config declares, nothing else."""

        if session.config is None:
            return []
        return session.config.enabled_skill_ids()


    def _tool_policy_for_session(self, session: Session) -> dict[str, Any]:
        if session.config and not session.config.is_empty():
            policy = session.config.tool_policy_dict()
            if policy.get("allowed_tools") or policy.get("approval_required_tools"):
                return policy
        return self._tool_policy()


    def _tool_policy_for_conversation(
        self,
        conversation: Conversation,
        session: Session,
    ) -> dict[str, Any]:
        """按对话模式调整运行工具策略。

        - default：沿用会话策略。
        - no_approval：无审批、不限工具（含 bash）、无工作区限制。
        - silent：静默模式，仅工作区白名单工具、路径受限、无审批。
        """
        policy = dict(self._tool_policy_for_session(session))
        mode = conversation.mode
        if mode == ConversationMode.NO_APPROVAL:
            policy["approval_required_tools"] = []
            policy["mode"] = mode.value
        elif mode == ConversationMode.SILENT:
            allowed = list(SILENT_MODE_TOOLS)
            if is_skill_generation_task(conversation.task):
                # Skill 提炼 agent 需要在写 SKILL.md 前向用户确认意图。
                allowed.append(ASK_USER_TOOL)
            policy["allowed_tools"] = allowed
            policy["approval_required_tools"] = []
            policy["mode"] = mode.value
        return policy


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
        self._schedule_publish(conversation.id, event)
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

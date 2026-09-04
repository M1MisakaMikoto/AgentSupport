"""Conversation and run operations: creation, core/temporal execution and release."""



from __future__ import annotations

import time
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4

from agent_runner_contracts.events import EventEnvelope

from ..domain import (
    TERMINAL_STATES,
    Checkpoint,
    Conversation,
    ConversationMode,
    ExecutionState,
    PresetSkill,
    Session,
)
from ..observability import metrics as obs_metrics
from .common import (
    ServiceError,
    _hash_request,
)
from .ports import (
    RepositoryConflict,
)


class ConversationOpsMixin:

    def get_conversation(self, conversation_id: UUID) -> Conversation:
        return self._conversation(conversation_id)


    def list_conversations(
        self,
        session_id: UUID | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Conversation]:
        if self.repository:
            return self.repository.list_conversations(
                session_id, limit=limit, offset=offset
            )
        items = self.conversations.values()
        if session_id is not None:
            items = [item for item in items if item.session_id == session_id]
        items = sorted(items, key=lambda item: item.created_at)
        if limit is not None:
            items = items[offset : offset + limit]
        return items


    async def create_conversation(
        self,
        session_id: UUID,
        task: str,
        parent_conversation_id: UUID | None = None,
        idempotency_key: str | None = None,
        *,
        workspace_id: UUID | None = None,
        skills: list[PresetSkill] | None = None,
        mcp_refs: list[dict[str, Any]] | None = None,
        auto_created: dict[str, Any] | None = None,
        mode: ConversationMode | None = None,
    ) -> Conversation:
        if mcp_refs is not None:
            self._validate_mcp_refs(mcp_refs)
        session = None if self.temporal_mode else self.sessions.get(session_id)
        if not session and self.repository:
            session = self.repository.get_session(session_id)
            if session:
                self.sessions[session.id] = session
        if not session:
            if not self._auto_create("session"):
                raise ServiceError("SESSION_NOT_FOUND", "session does not exist", 404)
            if workspace_id is None:
                raise ServiceError("SESSION_NOT_FOUND", "session does not exist", 404)
            session = self.create_session(
                workspace_id,
                idempotency_key=None,
                session_id=session_id,
                auto_created=auto_created,
            )
        if skills is not None:
            self._validate_skill_ids(
                [skill.skill_id for skill in skills], tenant_id=session.tenant_id
            )
        if parent_conversation_id:
            parent = None if self.temporal_mode else self.conversations.get(parent_conversation_id)
            if not parent and self.repository:
                parent = self.repository.get_conversation(parent_conversation_id)
                if parent:
                    self.conversations[parent.id] = parent
            if not parent or parent.session_id != session_id:
                raise ServiceError("PARENT_NOT_FOUND", "parent conversation is invalid", 404)
        payload = {
            "session_id": session_id,
            "task": task,
            "parent_conversation_id": parent_conversation_id,
            "mode": (mode or ConversationMode.DEFAULT).value,
        }
        existing = self._idempotent("conversation", idempotency_key, payload)
        if existing:
            return self.conversations[existing]
        conversation = Conversation(
            session_id=session_id,
            task=task,
            parent_conversation_id=parent_conversation_id,
            skills=skills,
            mcp_refs=mcp_refs,
            mode=mode or ConversationMode.DEFAULT,
        )
        if self.temporal is None:
            raise ServiceError(
                "TEMPORAL_UNAVAILABLE", "temporal coordinator is not configured", 503
            )
        assert self.repository is not None
        try:
            persisted = self.repository.create_conversation(
                conversation, _hash_request(payload), idempotency_key
            )
        except RepositoryConflict as exc:
            raise ServiceError("IDEMPOTENCY_CONFLICT", str(exc), 409) from exc
        if persisted.id != conversation.id:
            return persisted  # idempotent replay of an earlier request
        conversation = persisted
        await self._start_temporal_run(conversation, session)
        return conversation


    def _temporal_run_request(self, conversation: Conversation, session: Session) -> dict:
        """Build the workflow input consumed by the temporal activities."""

        skills = self._skills_for_conversation(conversation, session)
        return {
            "run_id": str(conversation.run.run_id),
            "conversation_id": str(conversation.id),
            "session_id": str(session.id),
            "workspace_id": str(session.workspace_id),
            "task": conversation.task,
            "skills": skills,
            "tool_policy": self._tool_policy_for_conversation(conversation, session),
            "mcp_refs": (
                [dict(item) for item in conversation.mcp_refs]
                if conversation.mcp_refs is not None
                else (
                    [dict(item) for item in session.config.resources.mcp_refs]
                    if session.config is not None and session.config.resources.mcp_refs
                    else []
                )
            ),
            "core_version": "0.1.0",
        }


    async def _start_temporal_run(self, conversation: Conversation, session: Session) -> None:
        if self.temporal is None:
            raise ServiceError(
                "TEMPORAL_UNAVAILABLE", "temporal coordinator is not configured", 503
            )
        request = self._temporal_run_request(conversation, session)
        try:
            await self.temporal.start_run(request)
        except Exception as exc:
            raise ServiceError(
                "TEMPORAL_START_FAILED",
                f"failed to start temporal run: {exc}",
                502,
            ) from exc


    async def _run_core(self, conversation: Conversation, session: Session) -> None:
        started = time.perf_counter()

        async def event_sink(event: EventEnvelope) -> None:
            self._apply_core_event(conversation, event)

        workspace = self.workspaces[session.workspace_id]
        workspace_ref = (
            str(
                PurePosixPath(
                    "/" + str(self.config.core_runner_workspace_root).replace("\\", "/").lstrip("/")
                )
                / Path(workspace.root_path).name
            )
            if self.config.core_runner_workspace_root
            else "/workspace"
        )
        runner_endpoint = await self._runner_endpoint(session)
        registered_runner = self.select_ready_runner({"run"})
        used_registered = False
        if registered_runner and (
            not runner_endpoint or runner_endpoint == self.config.core_runner_url
        ):
            runner_endpoint = registered_runner.endpoint
            used_registered = True
        if used_registered or (runner_endpoint and not self.config.core_runner_url):
            workspace_ref = "/workspace"
        tool_policy = self._tool_policy_for_conversation(conversation, session)
        conversation_skills = self._skills_for_conversation(conversation, session)
        request = {
            "run_id": str(conversation.run.run_id),
            "conversation_id": str(conversation.id),
            "session_id": str(session.id),
            "container_id": session.active_container_id or "unassigned",
            "lease_epoch": session.lease_epoch,
            "fence_epoch": session.lease_epoch,
            "correlation_id": str(uuid4()),
            "context_bundle": {
                "task": conversation.task,
                "conversation_id": str(conversation.id),
                "workspace_ref": workspace_ref,
                "recent_events": self._session_recent_events(session, conversation),
                "skill_manifest": self.skill_provider.manifest(
                    conversation_skills, tenant_id=session.tenant_id
                ),
                "skills": self.skill_provider.skill_prompt_entries(
                    conversation_skills, tenant_id=session.tenant_id
                ),
                "tool_policy": tool_policy,
                "file_ref_format": bool(
                    getattr(session.config, "file_ref_format", False)
                ),
                "mcp_refs": self._resolve_mcp_refs(conversation.mcp_refs, session),
            },
            "workspace_ref": workspace_ref,
            "tool_policy": tool_policy,
            "core_version": "0.1.0",
        }
        runner_endpoint = await self._register_core_endpoint(
            session, conversation.run.run_id, runner_endpoint
        )
        if runner_endpoint:
            request["runner_url"] = runner_endpoint
        try:
            await self.core_runtime.run(request, event_sink)
        except Exception as exc:  # noqa: BLE001 - runtime failures become AgentSupport events
            await self._cancel_runner_best_effort(conversation.run.run_id)
            self._mark_run_failed(
                conversation,
                {"code": "CORE_RUNTIME_ERROR", "message": str(exc)},
            )
        if conversation.run.state in TERMINAL_STATES:
            obs_metrics.record_run(
                conversation.run.state.value.lower(), time.perf_counter() - started
            )
            await self._release_session(session, conversation)


    def _mark_run_failed(self, conversation: Conversation, payload: dict[str, Any]) -> None:
        """Persist a structured run failure on the conversation projection."""

        conversation.run.state = ExecutionState.FAILED
        conversation.run.error = dict(payload)
        self._append(conversation, "run.failed", payload)


    def _apply_core_event(self, conversation: Conversation, event: EventEnvelope) -> None:
        if event.type == "interaction.requested":
            conversation.run.pending_interaction = event.payload
            conversation.run.state = ExecutionState.WAITING_INPUT
        elif event.type == "run.completed":
            conversation.run.pending_interaction = None
            conversation.run.state = ExecutionState.COMPLETED
            conversation.run.result_summary = event.payload.get("result", event.payload)
        elif event.type == "run.failed":
            self._mark_run_failed(conversation, event.payload)
        elif event.type == "run.lost":
            conversation.run.state = ExecutionState.LOST
        elif event.type == "run.cancelled":
            conversation.run.pending_interaction = None
            conversation.run.state = ExecutionState.CANCELLED
        self._append(conversation, event.type, event.payload, source=event.source)


    async def _forward_core_events(
        self, conversation: Conversation, response: dict[str, Any]
    ) -> None:
        for raw_event in response.get("events", []):
            self._apply_core_event(conversation, EventEnvelope.model_validate(raw_event))


    async def _release_session(
        self, session: Session, conversation: Conversation | None = None
    ) -> bool:
        if session.active_container_id:
            operation_id = (
                self.repository.create_runtime_operation("stop", session.active_container_id)
                if self.repository
                else None
            )
            try:
                stopped = await self.runtime_driver.stop(session.active_container_id)
            except Exception as exc:  # noqa: BLE001 - runtime failures keep the lease fenced
                if self.repository and operation_id:
                    self.repository.finish_runtime_operation(
                        operation_id, "FAILED", {"error": str(exc)}
                    )
                stopped = False
            if not stopped:
                if self.repository and operation_id:
                    self.repository.finish_runtime_operation(
                        operation_id, "FAILED", {"error": "container stop unconfirmed"}
                    )
                if conversation:
                    self._append(
                        conversation,
                        "container_stop_unconfirmed",
                        {"container_id": session.active_container_id},
                    )
                return False
            if self.repository and operation_id:
                self.repository.finish_runtime_operation(operation_id, "SUCCEEDED", {})
        session.active_container_id = None
        session.active_run_id = None
        if conversation and self.core_runtime is not None:
            unregister = getattr(self.core_runtime, "unregister_run_endpoint", None)
            if unregister is not None:
                unregister(conversation.run.run_id)
        if self.repository:
            self.repository.release_session_leases(session)
            self.repository.save_session(session)
        if self.workspace_leases.get(session.workspace_id) == session.id:
            self.workspace_leases.pop(session.workspace_id, None)
        await self._drain_session_queue(session)
        return True


    async def _drain_session_queue(self, session: Session) -> None:
        queued = sorted(
            (
                item
                for item in self.conversations.values()
                if item.run.state == ExecutionState.QUEUED
            ),
            key=lambda item: item.created_at,
        )
        for conversation in queued:
            candidate = self.sessions.get(conversation.session_id)
            if candidate is None and self.repository:
                candidate = self.repository.get_session(conversation.session_id)
                if candidate:
                    self.sessions[candidate.id] = candidate
            if candidate is None or candidate.active_run_id:
                continue
            if not await self._acquire_container(candidate):
                continue
            candidate.active_run_id = conversation.run.run_id
            if self.repository:
                self.repository.save_session(candidate)
            conversation.run.state = ExecutionState.STARTING
            self._append(conversation, "run.started", {"session_id": str(candidate.id)})
            conversation.run.state = ExecutionState.RUNNING
            self._append(
                conversation, "run.running", {"container_id": candidate.active_container_id}
            )
            if self.core_runtime:
                await self._run_core(conversation, candidate)
            return


    def _conversation(self, conversation_id: UUID) -> Conversation:
        conversation = None if self.temporal_mode else self.conversations.get(conversation_id)
        if not conversation and self.repository:
            conversation = self.repository.get_conversation(conversation_id)
            if conversation and not self.temporal_mode:
                self.conversations[conversation.id] = conversation
                events = self.repository.list_events(conversation.id)
                existing = self.events_store.list(conversation.id)
                last_seq = existing[-1].seq if existing else 0
                for event in events:
                    if event.seq > last_seq:
                        self.events_store.append(conversation.id, event)
        if not conversation:
            raise ServiceError("CONVERSATION_NOT_FOUND", "conversation does not exist", 404)
        return conversation


    def _checkpoint(self, conversation: Conversation) -> Checkpoint | None:
        checkpoint_id = conversation.run.checkpoint_id
        if checkpoint_id is None:
            return None
        checkpoint = self.checkpoints.get(checkpoint_id)
        if checkpoint is None and self.repository:
            checkpoint = self.repository.get_checkpoint(checkpoint_id)
            if checkpoint:
                self.checkpoints[checkpoint.checkpoint_id] = checkpoint
        return checkpoint




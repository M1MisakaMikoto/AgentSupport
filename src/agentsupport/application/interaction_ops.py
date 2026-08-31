"""Interaction, approval, pause, resume and cancel operations."""



from __future__ import annotations

from typing import Any
from uuid import UUID

from agent_runner_contracts.checkpoint import (
    tool_policy_hash,
    validate_checkpoint,
)
from agent_runner_contracts.events import EventEnvelope

from ..domain import (
    TERMINAL_STATES,
    Conversation,
    ExecutionState,
)
from .common import (
    ServiceError,
    _hash_request,
)
from .ports import (
    RepositoryConflict,
)


class InteractionOpsMixin:

    def request_interaction(
        self, conversation_id: UUID, interaction: dict[str, Any]
    ) -> Conversation:
        conversation = self._conversation(conversation_id)
        if conversation.run.state != ExecutionState.RUNNING:
            raise ServiceError("INVALID_STATE", "interaction requires RUNNING conversation", 409)
        conversation.run.pending_interaction = interaction
        conversation.run.state = ExecutionState.WAITING_INPUT
        self._append(conversation, "interaction.requested", interaction)
        return conversation


    async def submit_input(
        self,
        conversation_id: UUID,
        interaction_id: str,
        value: Any,
        expected_seq: int | None = None,
        idempotency_key: str | None = None,
    ) -> Conversation:
        conversation = self._conversation(conversation_id)
        payload = {
            "conversation_id": conversation_id,
            "interaction_id": interaction_id,
            "value": value,
            "expected_seq": expected_seq,
        }
        if self.temporal_mode:
            if self.temporal is None:
                raise ServiceError(
                    "TEMPORAL_UNAVAILABLE", "temporal coordinator is not configured", 503
                )
            if self.repository and idempotency_key:
                existing = self.repository.find_idempotent(
                    "input", idempotency_key, _hash_request(payload)
                )
                if existing:
                    return self._conversation(existing)
            self._check_expected_seq(conversation, expected_seq)
            self._check_interaction(conversation, interaction_id)
            try:
                await self.temporal.submit_input(
                    str(conversation.run.run_id),
                    interaction_id,
                    value,
                    idempotency_key,
                )
            except Exception as exc:
                raise ServiceError(
                    "TEMPORAL_SIGNAL_FAILED", f"failed to deliver input: {exc}", 409
                ) from exc
            self._remember_persisted(
                "input",
                idempotency_key,
                payload,
                conversation.id,
                conversation.model_dump(mode="json"),
            )
            return conversation
        existing = self._idempotent("input", idempotency_key, payload)
        if existing:
            return self._conversation(existing)
        if self.repository and idempotency_key:
            try:
                existing = self.repository.find_idempotent(
                    "input", idempotency_key, _hash_request(payload)
                )
            except RepositoryConflict as exc:
                raise ServiceError("IDEMPOTENCY_CONFLICT", str(exc), 409) from exc
            if existing:
                self._remember("input", idempotency_key, payload, existing)
                return self._conversation(existing)
        self._check_expected_seq(conversation, expected_seq)
        self._check_interaction(conversation, interaction_id)
        was_paused = conversation.run.state == ExecutionState.PAUSED
        checkpoint = self._checkpoint(conversation) if was_paused else None
        if was_paused:
            if checkpoint is None:
                raise ServiceError(
                    "CHECKPOINT_NOT_FOUND", "conversation checkpoint is missing", 409
                )
            try:
                validate_checkpoint(
                    checkpoint,
                    lease_epoch=self.sessions[conversation.session_id].lease_epoch,
                    expected_tool_policy_hash=tool_policy_hash(
                        self._tool_policy_for_conversation(
                            conversation, self.sessions[conversation.session_id]
                        )
                    ),
                )
            except ValueError as exc:
                raise ServiceError("CHECKPOINT_INVALID", str(exc), 409) from exc
        if conversation.run.state == ExecutionState.PAUSED:
            await self._resume_session(conversation)
        conversation.run.pending_interaction = None
        conversation.run.state = ExecutionState.RUNNING
        self._append(
            conversation, "interaction.input", {"interaction_id": interaction_id, "value": value}
        )
        if self.core_runtime:
            try:
                await self._register_core_endpoint(
                    self.sessions[conversation.session_id], conversation.run.run_id
                )
                if was_paused:

                    async def sink(event: EventEnvelope) -> None:
                        self._apply_core_event(conversation, event)

                    response = await self.core_runtime.resume(checkpoint, value, sink)
                    response = {**response, "events": []}
                else:
                    response = await self.core_runtime.accept_input(
                        conversation.run.run_id, interaction_id, value
                    )
                await self._forward_core_events(conversation, response)
            except ServiceError:
                raise
            except Exception as exc:  # noqa: BLE001 - runtime errors become run failures
                await self._cancel_runner_best_effort(conversation.run.run_id)
                self._mark_run_failed(
                    conversation,
                    {"code": "CORE_RUNTIME_ERROR", "message": str(exc)},
                )
        if conversation.run.state in TERMINAL_STATES:
            await self._release_session(self.sessions[conversation.session_id], conversation)
        self._remember_persisted(
            "input", idempotency_key, payload, conversation.id, conversation.model_dump(mode="json")
        )
        return conversation


    async def submit_approval(
        self,
        conversation_id: UUID,
        approval_id: str,
        decision: str,
        expected_seq: int | None = None,
        idempotency_key: str | None = None,
    ) -> Conversation:
        if decision not in {"APPROVE_ONCE", "REJECT"}:
            raise ServiceError("INVALID_DECISION", "decision must be APPROVE_ONCE or REJECT", 422)
        conversation = self._conversation(conversation_id)
        payload = {
            "conversation_id": conversation_id,
            "approval_id": approval_id,
            "decision": decision,
            "expected_seq": expected_seq,
        }
        if self.temporal_mode:
            if self.temporal is None:
                raise ServiceError(
                    "TEMPORAL_UNAVAILABLE", "temporal coordinator is not configured", 503
                )
            if self.repository and idempotency_key:
                existing = self.repository.find_idempotent(
                    "approval", idempotency_key, _hash_request(payload)
                )
                if existing:
                    return self._conversation(existing)
            self._check_expected_seq(conversation, expected_seq)
            self._check_interaction(conversation, approval_id)
            try:
                await self.temporal.submit_approval(
                    str(conversation.run.run_id),
                    approval_id,
                    decision,
                    idempotency_key,
                )
            except Exception as exc:
                raise ServiceError(
                    "TEMPORAL_SIGNAL_FAILED", f"failed to deliver approval: {exc}", 409
                ) from exc
            self._remember_persisted(
                "approval",
                idempotency_key,
                payload,
                conversation.id,
                conversation.model_dump(mode="json"),
            )
            return conversation
        existing = self._idempotent("approval", idempotency_key, payload)
        if existing:
            return self._conversation(existing)
        if self.repository and idempotency_key:
            try:
                existing = self.repository.find_idempotent(
                    "approval", idempotency_key, _hash_request(payload)
                )
            except RepositoryConflict as exc:
                raise ServiceError("IDEMPOTENCY_CONFLICT", str(exc), 409) from exc
            if existing:
                self._remember("approval", idempotency_key, payload, existing)
                return self._conversation(existing)
        self._check_expected_seq(conversation, expected_seq)
        self._check_interaction(conversation, approval_id)
        was_paused = conversation.run.state == ExecutionState.PAUSED
        checkpoint = self._checkpoint(conversation) if was_paused else None
        if was_paused:
            if checkpoint is None:
                raise ServiceError(
                    "CHECKPOINT_NOT_FOUND", "conversation checkpoint is missing", 409
                )
            try:
                validate_checkpoint(
                    checkpoint,
                    lease_epoch=self.sessions[conversation.session_id].lease_epoch,
                    expected_tool_policy_hash=tool_policy_hash(
                        self._tool_policy_for_conversation(
                            conversation, self.sessions[conversation.session_id]
                        )
                    ),
                )
            except ValueError as exc:
                raise ServiceError("CHECKPOINT_INVALID", str(exc), 409) from exc
        if conversation.run.state == ExecutionState.PAUSED:
            await self._resume_session(conversation)
        conversation.run.pending_interaction = None
        conversation.run.state = ExecutionState.RUNNING
        self._append(
            conversation, "approval.decided", {"approval_id": approval_id, "decision": decision}
        )
        if self.core_runtime:
            try:
                await self._register_core_endpoint(
                    self.sessions[conversation.session_id], conversation.run.run_id
                )
                if was_paused:

                    async def sink(event: EventEnvelope) -> None:
                        self._apply_core_event(conversation, event)

                    response = await self.core_runtime.resume(checkpoint, decision, sink)
                    response = {**response, "events": []}
                else:
                    response = await self.core_runtime.accept_approval(
                        conversation.run.run_id, approval_id, decision
                    )
                await self._forward_core_events(conversation, response)
            except ServiceError:
                raise
            except Exception as exc:  # noqa: BLE001 - runtime errors become run failures
                await self._cancel_runner_best_effort(conversation.run.run_id)
                self._mark_run_failed(
                    conversation,
                    {"code": "CORE_RUNTIME_ERROR", "message": str(exc)},
                )
        if conversation.run.state in TERMINAL_STATES:
            await self._release_session(self.sessions[conversation.session_id], conversation)
        self._remember_persisted(
            "approval",
            idempotency_key,
            payload,
            conversation.id,
            conversation.model_dump(mode="json"),
        )
        return conversation


    def _check_expected_seq(self, conversation: Conversation, expected_seq: int | None) -> None:
        if expected_seq is not None and expected_seq != conversation.run.last_seq:
            raise ServiceError("CONFLICT", "expected_seq does not match conversation", 409)


    def _check_interaction(self, conversation: Conversation, interaction_id: str) -> None:
        pending = conversation.run.pending_interaction
        if (
            conversation.run.state not in {ExecutionState.WAITING_INPUT, ExecutionState.PAUSED}
            or not pending
        ):
            raise ServiceError("INVALID_STATE", "conversation is not waiting for input", 409)
        if pending.get("interaction_id") != interaction_id:
            raise ServiceError(
                "INTERACTION_NOT_FOUND", "interaction does not match pending interaction", 409
            )


    async def pause(
        self, conversation_id: UUID, reason: str = "waiting_input_timeout"
    ) -> Conversation:
        conversation = self._conversation(conversation_id)
        if conversation.run.state != ExecutionState.WAITING_INPUT:
            raise ServiceError("INVALID_STATE", "only WAITING_INPUT can be paused", 409)
        conversation.run.state = ExecutionState.SUSPENDING
        self._append(conversation, "checkpoint.requested", {"reason": reason})
        if self.core_runtime:
            await self.core_runtime.checkpoint(conversation.run.run_id, reason)
        self.create_checkpoint(conversation_id, reason)
        session = self.sessions[conversation.session_id]
        if not await self._release_session(session, conversation):
            return conversation
        conversation.run.state = ExecutionState.PAUSED
        self._append(conversation, "run.paused", {"reason": reason})
        return conversation


    async def _resume_session(self, conversation: Conversation) -> None:
        session = self.sessions[conversation.session_id]
        if not session.active_container_id and not await self._acquire_container(session):
            raise ServiceError("RESOURCE_EXHAUSTED", "session container capacity is exhausted", 429)
        session.active_run_id = conversation.run.run_id
        await self._register_core_endpoint(session, conversation.run.run_id)
        if self.repository:
            self.repository.save_session(session)


    async def cancel(
        self,
        conversation_id: UUID,
        expected_seq: int | None = None,
        idempotency_key: str | None = None,
    ) -> Conversation:
        conversation = self._conversation(conversation_id)
        payload = {"conversation_id": conversation_id, "expected_seq": expected_seq}
        if self.temporal_mode:
            if self.temporal is None:
                raise ServiceError(
                    "TEMPORAL_UNAVAILABLE", "temporal coordinator is not configured", 503
                )
            if conversation.run.state in TERMINAL_STATES:
                self._remember_persisted(
                    "cancel",
                    idempotency_key,
                    payload,
                    conversation.id,
                    conversation.model_dump(mode="json"),
                )
                return conversation
            self._check_expected_seq(conversation, expected_seq)
            try:
                await self.temporal.cancel(str(conversation.run.run_id))
            except Exception as exc:
                raise ServiceError(
                    "TEMPORAL_SIGNAL_FAILED", f"failed to deliver cancel: {exc}", 409
                ) from exc
            self._remember_persisted(
                "cancel",
                idempotency_key,
                payload,
                conversation.id,
                conversation.model_dump(mode="json"),
            )
            return conversation
        existing = self._idempotent("cancel", idempotency_key, payload)
        if existing:
            return self._conversation(existing)
        if self.repository and idempotency_key:
            try:
                existing = self.repository.find_idempotent(
                    "cancel", idempotency_key, _hash_request(payload)
                )
            except RepositoryConflict as exc:
                raise ServiceError("IDEMPOTENCY_CONFLICT", str(exc), 409) from exc
            if existing:
                self._remember("cancel", idempotency_key, payload, existing)
                return self._conversation(existing)
        if conversation.run.state in TERMINAL_STATES:
            self._remember_persisted(
                "cancel",
                idempotency_key,
                payload,
                conversation.id,
                conversation.model_dump(mode="json"),
            )
            return conversation
        self._check_expected_seq(conversation, expected_seq)
        if self.core_runtime and conversation.run.state in {
            ExecutionState.RUNNING,
            ExecutionState.WAITING_INPUT,
        }:
            try:
                await self._register_core_endpoint(
                    self.sessions[conversation.session_id], conversation.run.run_id
                )
                response = await self.core_runtime.cancel(conversation.run.run_id)
                await self._forward_core_events(conversation, response)
            except Exception as exc:  # noqa: BLE001 - runtime errors become run failures
                await self._cancel_runner_best_effort(conversation.run.run_id)
                self._mark_run_failed(
                    conversation,
                    {"code": "CORE_RUNTIME_ERROR", "message": str(exc)},
                )
        if conversation.run.state not in TERMINAL_STATES:
            conversation.run.pending_interaction = None
            conversation.run.state = ExecutionState.CANCELLED
            self._append(conversation, "run.cancelled", {})
        session = self.sessions[conversation.session_id]
        if session.active_run_id == conversation.run.run_id:
            await self._release_session(session, conversation)
        self._remember_persisted(
            "cancel",
            idempotency_key,
            payload,
            conversation.id,
            conversation.model_dump(mode="json"),
        )
        return conversation

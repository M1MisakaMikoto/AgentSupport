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

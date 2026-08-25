"""Checkpoint creation over the canonical conversation state."""



from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from agent_runner_contracts.checkpoint import (
    RECENT_EVENTS_LIMIT,
    tool_policy_hash,
    tool_versions_hash,
)
from agent_runner_contracts.events import EventEnvelope
from agent_runner_contracts.tools import ToolBatch

from ..domain import (
    Checkpoint,
    ContextBundle,
)
from .common import (
    ServiceError,
)
from .ports import (
    RepositoryConflict,
)


class CheckpointOpsMixin:

    def create_checkpoint(self, conversation_id: UUID, reason: str) -> Checkpoint:
        conversation = self._conversation(conversation_id)
        session = self.sessions[conversation.session_id]
        pending_interaction = conversation.run.pending_interaction
        pending_tool_calls: list[dict[str, Any]] = []
        tool_batch_hash: str | None = None
        if pending_interaction:
            raw_batch = pending_interaction.get("tool_batch")
            if isinstance(raw_batch, dict):
                try:
                    batch = ToolBatch.model_validate(raw_batch)
                except Exception as exc:
                    raise ServiceError(
                        "CHECKPOINT_INVALID", "pending tool batch is invalid", 409
                    ) from exc
                pending_tool_calls = [call.model_dump(mode="json") for call in batch.calls]
                tool_batch_hash = batch.batch_hash
            else:
                pending_tool_calls = list(pending_interaction.get("pending_tool_calls", []))
                tool_batch_hash = pending_interaction.get("tool_batch_hash")
        raw_tool_policy = pending_interaction.get("tool_policy", {}) if pending_interaction else {}
        tool_policy: dict[str, Any] = self._tool_policy_for_session(session)
        tool_policy.update(raw_tool_policy)
        tool_policy["reason"] = reason
        if pending_tool_calls:
            tool_policy["pending_tool_calls"] = pending_tool_calls
        if tool_batch_hash:
            tool_policy["tool_batch_hash"] = tool_batch_hash
        checkpoint_tool_policy_hash = tool_policy_hash(tool_policy)
        raw_tools = list(tool_policy.get("tools", []))
        checkpoint_tool_versions_hash = tool_versions_hash(raw_tools) if raw_tools else None
        tool_policy["tool_policy_hash"] = checkpoint_tool_policy_hash
        if checkpoint_tool_versions_hash:
            tool_policy["tool_versions_hash"] = checkpoint_tool_versions_hash
        context = ContextBundle(
            task=conversation.task,
            conversation_id=conversation.id,
            workspace_ref="/workspace",
            recent_events=[
                event.model_dump(mode="json")
                for event in self.events_store.list(conversation.id)
            ][-RECENT_EVENTS_LIMIT:],
            tool_policy=tool_policy,
        )
        context_hash = hashlib.sha256(
            json.dumps(
                context.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        checkpoint = Checkpoint(
            conversation_id=conversation.id,
            run_id=conversation.run.run_id,
            last_event_seq=conversation.run.last_seq,
            context_bundle=context,
            pending_interaction=pending_interaction,
            pending_tool_calls=pending_tool_calls,
            tool_batch_hash=tool_batch_hash,
            tool_policy_hash=checkpoint_tool_policy_hash,
            tool_versions_hash=checkpoint_tool_versions_hash,
            workspace_ref="/workspace",
            workspace_write_lease_epoch=session.lease_epoch,
            context_bundle_hash=context_hash,
        )
        self.checkpoints[checkpoint.checkpoint_id] = checkpoint
        conversation.run.checkpoint_id = checkpoint.checkpoint_id
        event = EventEnvelope(
            run_id=conversation.run.run_id,
            seq=conversation.run.last_seq + 1,
            type="checkpoint.created",
            payload={"checkpoint_id": str(checkpoint.checkpoint_id)},
            source="agentsupport",
        )
        if self.repository:
            try:
                self.repository.save_checkpoint_and_append_event(checkpoint, conversation, event)
            except RepositoryConflict as exc:
                raise ServiceError("CHECKPOINT_CONFLICT", str(exc), 409) from exc
        self.events_store.append(conversation.id, event)
        conversation.run.last_seq = event.seq
        self._schedule_publish(conversation.id, event)
        return checkpoint

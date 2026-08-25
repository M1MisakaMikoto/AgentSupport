"""Exp 05: checkpoint combined with Temporal.

Temporal durably stores workflow state (the event history), so the data that
the current Checkpoint serializes -- the ContextBundle (task, conversation,
recent events, tool policy, pending tool calls) -- can live directly in the
workflow and be handed to a retried activity.  This is the part a plain
Temporal run cannot invent on its own (the Agent session memory lives in the
runner), so the "checkpoint bundle" stays as a data contract.

The experiment fails the activity once (simulated container crash) and proves
the retried attempt receives the byte-identical bundle.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from common import SideEffectStore, fresh_store



@activity.defn
async def run_agent_with_bundle(bundle: dict, side_effect_path: str) -> str:
    store = SideEffectStore(side_effect_path)
    bundle_hash = hashlib.sha256(
        json.dumps(bundle, sort_keys=True, default=str).encode()
    ).hexdigest()
    attempt = store.append("run_agent_with_bundle", bundle_hash)
    if attempt == 1:
        raise RuntimeError("simulated container crash")
    return f"finished task={bundle['task']} events={len(bundle['recent_events'])}"


@workflow.defn
class CheckpointCombinedWorkflow:
    def __init__(self) -> None:
        self._bundle: dict | None = None

    @workflow.run
    async def run(self, task: str, side_effect_path: str) -> str:
        # This dict is the descendant of agent_runner_contracts ContextBundle.
        # Being workflow state, it is durable without a checkpoint table.
        self._bundle = {
            "task": task,
            "conversation_id": "conv-exp05",
            "workspace_ref": "/workspace",
            "recent_events": ["event-1", "event-2"],
            "tool_policy": {"allowed_tools": ["bash", "edit"]},
            "pending_tool_calls": [],
        }
        result = await workflow.execute_activity(
            run_agent_with_bundle,
            args=[self._bundle, side_effect_path],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                maximum_attempts=2, initial_interval=timedelta(seconds=1)
            ),
        )
        return result

    @workflow.query
    def context_bundle(self) -> dict | None:
        return self._bundle


async def run(evidence_dir: Path) -> str:
    from common import TASK_QUEUE, connect
    from uuid import uuid4

    store_path = evidence_dir / "exp05_checkpoint_combined.json"
    fresh_store(store_path)
    client = await connect()
    handle = await client.start_workflow(
        CheckpointCombinedWorkflow.run,
        args=["refactor the gateway", str(store_path)],
        id=f"exp05-checkpoint-combined-{uuid4().hex[:8]}",
        task_queue=TASK_QUEUE,
    )
    result = await handle.result()
    assert result == "finished task=refactor the gateway events=2"

    store = SideEffectStore(store_path)
    hashes = store.details("run_agent_with_bundle")
    assert len(hashes) == 2, "expected exactly one failed attempt + one retry"
    assert hashes[0] == hashes[1], "retried activity got a different bundle"

    bundle = await handle.query(CheckpointCombinedWorkflow.context_bundle)
    assert bundle is not None and bundle["task"] == "refactor the gateway"
    return (
        "checkpoint bundle lives in workflow state (durable, queryable); "
        "retried activity received the byte-identical bundle -- Temporal "
        "orchestration + checkpoint data contract combined"
    )

WORKFLOWS = [CheckpointCombinedWorkflow]
ACTIVITIES = [run_agent_with_bundle]

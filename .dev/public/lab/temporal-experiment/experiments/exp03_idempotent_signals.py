"""Exp 03: idempotent commands delivered as signals.

Current AgentSupport: RunCommand rows with an idempotency_key and a PENDING ->
CLAIMED -> APPLIED state machine.

Temporal: signals are at-least-once and are durably buffered by the server
even before the workflow starts waiting.  Combined with a keyed guard inside
the workflow, a replayed command becomes a no-op -- exactly-once effect.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from common import SideEffectStore, fresh_store



@activity.defn
async def apply_input(value: str, side_effect_path: str) -> str:
    store = SideEffectStore(side_effect_path)
    store.append("apply_input", value)
    await asyncio.sleep(0.2)
    return f"applied:{value}"


@workflow.defn
class IdempotentInputWorkflow:
    def __init__(self) -> None:
        self._input: str | None = None
        self._keys: set[str] = set()

    @workflow.run
    async def run(self, side_effect_path: str) -> str:
        # Signals can arrive before this wait: Temporal buffers them durably
        # and replays them here -- replacing the RunCommand queue.
        await workflow.wait_condition(lambda: self._input is not None)
        result = await workflow.execute_activity(
            apply_input,
            args=[self._input, side_effect_path],
            start_to_close_timeout=timedelta(seconds=30),
        )
        return result

    @workflow.signal
    async def submit_input(self, value: str, idempotency_key: str) -> None:
        if idempotency_key in self._keys:
            return
        self._keys.add(idempotency_key)
        self._input = value


async def run(evidence_dir: Path) -> str:
    from common import TASK_QUEUE, connect
    from uuid import uuid4

    store_path = evidence_dir / "exp03_idempotent_signals.json"
    fresh_store(store_path)
    client = await connect()
    handle = await client.start_workflow(
        IdempotentInputWorkflow.run,
        str(store_path),
        id=f"exp03-idempotent-signals-{uuid4().hex[:8]}",
        task_queue=TASK_QUEUE,
    )
    # Deliver the command TWICE with the same key, immediately after start --
    # before the workflow is even waiting (tests durable buffering too).
    await handle.signal(IdempotentInputWorkflow.submit_input, args=["approve", "cmd-1"])
    await handle.signal(IdempotentInputWorkflow.submit_input, args=["deny", "cmd-1"])

    result = await handle.result()
    assert result == "applied:approve"
    store = SideEffectStore(store_path)
    assert store.count("apply_input") == 1, "duplicate command was applied twice"
    return (
        "duplicate signal with same idempotency_key applied exactly once; "
        "early signals are durably buffered (RunCommand queue replaced)"
    )

WORKFLOWS = [IdempotentInputWorkflow]
ACTIVITIES = [apply_input]

"""Exp 02: cancellation (mirrors RunCommand.cancel).

Two flavours are demonstrated:
  A) workflow-level cancel (handle.cancel()) -> the activity receives the
     cancellation on its next heartbeat, workflow ends CANCELLED;
  B) signal-driven cancel -> the workflow asks the activity to stop and
     finishes gracefully.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from pathlib import Path

from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from common import SideEffectStore, fresh_store



@activity.defn
async def long_agent_run(side_effect_path: str) -> str:
    store = SideEffectStore(side_effect_path)
    store.append("long_run")
    for _ in range(120):
        activity.heartbeat("agent still working")
        await asyncio.sleep(0.25)
    store.append("long_run_done")
    return "completed-naturally"


@workflow.defn
class CancelViaWorkflowApiWorkflow:
    @workflow.run
    async def run(self, side_effect_path: str) -> str:
        return await workflow.execute_activity(
            long_agent_run,
            side_effect_path,
            start_to_close_timeout=timedelta(minutes=5),
            heartbeat_timeout=timedelta(seconds=5),
        )


@workflow.defn
class CancelViaSignalWorkflow:
    def __init__(self) -> None:
        self._cancel_requested = False

    @workflow.run
    async def run(self, side_effect_path: str) -> str:
        task = asyncio.create_task(
            workflow.execute_activity(
                long_agent_run,
                side_effect_path,
                start_to_close_timeout=timedelta(minutes=5),
                heartbeat_timeout=timedelta(seconds=5),
            )
        )
        await workflow.wait_condition(lambda: self._cancel_requested)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, ActivityError):
            await task
        return "run-cancelled-gracefully"

    @workflow.signal
    async def cancel_run(self) -> None:
        self._cancel_requested = True


async def _wait_for_activity(client, wf_id: str, timeout: float = 30) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        handle = client.get_workflow_handle(wf_id)
        desc = await handle.describe()
        if desc.raw_description.pending_activities:
            return
        await asyncio.sleep(0.2)
    raise AssertionError("activity never started")


async def run(evidence_dir: Path) -> str:
    from common import TASK_QUEUE, connect
    from uuid import uuid4

    client = await connect()
    lines: list[str] = []

    # --- A: workflow-level cancel -> CANCELLED terminal state ---
    store_a = evidence_dir / "exp02_cancel_workflow_api.json"
    fresh_store(store_a)
    handle_a = await client.start_workflow(
        CancelViaWorkflowApiWorkflow.run,
        str(store_a),
        id=f"exp02-cancel-api-{uuid4().hex[:8]}",
        task_queue=TASK_QUEUE,
    )
    await _wait_for_activity(client, handle_a.id)
    await handle_a.cancel()
    try:
        await handle_a.result()
        raise AssertionError("cancelled workflow unexpectedly completed")
    except WorkflowFailureError as exc:
        from temporalio.exceptions import CancelledError

        assert isinstance(exc.cause, CancelledError)
    store_a = SideEffectStore(store_a)
    assert store_a.count("long_run") == 1
    assert store_a.count("long_run_done") == 0, "activity finished after cancel"
    lines.append("workflow cancel -> CANCELLED, activity stopped via heartbeat")

    # --- B: signal-driven graceful cancel ---
    store_b = evidence_dir / "exp02_cancel_signal.json"
    fresh_store(store_b)
    handle_b = await client.start_workflow(
        CancelViaSignalWorkflow.run,
        str(store_b),
        id=f"exp02-cancel-signal-{uuid4().hex[:8]}",
        task_queue=TASK_QUEUE,
    )
    await _wait_for_activity(client, handle_b.id)
    await handle_b.signal(CancelViaSignalWorkflow.cancel_run)
    result = await handle_b.result()
    assert result == "run-cancelled-gracefully"
    store_b = SideEffectStore(store_b)
    assert store_b.count("long_run_done") == 0
    lines.append("signal cancel -> graceful stop, activity not completed")
    return "; ".join(lines)

WORKFLOWS = [CancelViaWorkflowApiWorkflow, CancelViaSignalWorkflow]
ACTIVITIES = [long_agent_run]

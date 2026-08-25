"""Exp 06: heartbeat timeout replaces lease/heartbeat expiry + reconciler.

Current AgentSupport: job lease renewal, runner heartbeat registrations and a
reconciler that polls for expired runners / orphan containers.

Temporal: an activity that stops heartbeating is failed by the server within
`heartbeat_timeout` -- no polling loop.  A hung runner is therefore detected
and surfaced as a TimeoutError without any custom reconciler.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from pathlib import Path

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, TimeoutError



@activity.defn
async def hung_runner() -> str:
    activity.heartbeat("alive-now")
    await asyncio.sleep(60)  # stops heartbeating -> heartbeat timeout
    return "finished"


@workflow.defn
class HeartbeatTimeoutWorkflow:
    @workflow.run
    async def run(self) -> str:
        try:
            await workflow.execute_activity(
                hung_runner,
                start_to_close_timeout=timedelta(minutes=2),
                heartbeat_timeout=timedelta(seconds=3),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except ActivityError as exc:
            return f"failed:{type(exc.cause).__name__}"
        return "unexpected-success"


async def run(evidence_dir: Path) -> str:
    from common import TASK_QUEUE, connect
    from uuid import uuid4

    client = await connect()
    started = time.monotonic()
    handle = await client.start_workflow(
        HeartbeatTimeoutWorkflow.run,
        id=f"exp06-heartbeat-timeout-{uuid4().hex[:8]}",
        task_queue=TASK_QUEUE,
    )
    result = await handle.result()
    elapsed = time.monotonic() - started
    assert result.startswith("failed:TimeoutError"), f"unexpected: {result}"
    assert elapsed < 15, f"detection took too long: {elapsed:.1f}s"
    return (
        f"hung runner detected in {elapsed:.1f}s via heartbeat timeout "
        "(no reconciler poll needed)"
    )

WORKFLOWS = [HeartbeatTimeoutWorkflow]
ACTIVITIES = [hung_runner]

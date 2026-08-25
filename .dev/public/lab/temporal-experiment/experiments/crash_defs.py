"""Workflow + activities used by the crash-recovery experiment."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from common import SideEffectStore


@activity.defn
async def step_a(side_effect_path: str) -> str:
    SideEffectStore(side_effect_path).append("step_a")
    await asyncio.sleep(0.3)
    return "step-a-done"


@activity.defn
async def step_b(side_effect_path: str) -> str:
    store = SideEffectStore(side_effect_path)
    store.append("step_b")
    for i in range(10):
        activity.heartbeat(f"progress {i}")
        await asyncio.sleep(0.5)
    store.append("step_b_done")
    return "step-b-done"


@workflow.defn
class CrashRecoveryWorkflow:
    @workflow.run
    async def run(self, side_effect_path: str) -> str:
        a = await workflow.execute_activity(
            step_a,
            side_effect_path,
            start_to_close_timeout=timedelta(seconds=30),
        )
        b = await workflow.execute_activity(
            step_b,
            side_effect_path,
            start_to_close_timeout=timedelta(seconds=60),
            heartbeat_timeout=timedelta(seconds=3),
            retry_policy=RetryPolicy(
                maximum_attempts=10, initial_interval=timedelta(seconds=1)
            ),
        )
        return f"{a}+{b}"

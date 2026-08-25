"""Smoke test: prove the lab can reach Temporal and run a trivial workflow."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    from common import NAMESPACE, TASK_QUEUE, address, connect


@activity.defn
async def greet(name: str) -> str:
    await asyncio.sleep(0.1)
    return f"hello {name}"


@workflow.defn
class GreetWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_activity(
            greet, name, start_to_close_timeout=timedelta(seconds=10)
        )


async def main() -> None:
    client = await connect()
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[GreetWorkflow],
        activities=[greet],
    )
    worker_task = asyncio.create_task(worker.run())
    try:
        handle = await client.start_workflow(
            GreetWorkflow.run, "lab", id="verify-setup", task_queue=TASK_QUEUE
        )
        result = await handle.result()
        assert result == "hello lab", result
        print(f"setup OK: Temporal reachable at {address()}, workflow ran -> {result}")
    finally:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())

"""Standalone Temporal worker subprocess for the crash-recovery experiment."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from temporalio.client import Client  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from common import NAMESPACE, TASK_QUEUE, address  # noqa: E402
from crash_defs import CrashRecoveryWorkflow, step_a, step_b  # noqa: E402


async def main() -> None:
    client = await Client.connect(address(), namespace=NAMESPACE)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[CrashRecoveryWorkflow],
        activities=[step_a, step_b],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())

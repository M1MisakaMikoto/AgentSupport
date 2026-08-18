"""Temporal worker entry point.

Usage::

    python -m agentsupport.execution.temporal.worker
"""

from __future__ import annotations

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from ...bootstrap.settings import settings
from ...observability import logging as obs_logging
from .activities import execute_run, fail_run, resume_run, start_runner, stop_runner
from .workflows import RunSessionWorkflow


async def main() -> None:
    obs_logging.configure_logging(
        log_format=settings.log_format,
        level=settings.log_level,
        service_name=settings.service_name,
    )
    client = await Client.connect(
        settings.temporal_host, namespace=settings.temporal_namespace
    )
    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[RunSessionWorkflow],
        activities=[start_runner, execute_run, resume_run, stop_runner, fail_run],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())

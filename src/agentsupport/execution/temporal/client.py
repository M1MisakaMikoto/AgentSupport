"""Control-plane client for the Temporal execution backend."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio.client import Client, WorkflowFailureError


class TemporalRunCoordinator:
    """Starts run workflows and delivers input/approval/cancel signals.

    Replaces the ExecutionJob/RunCommand repository plumbing.  The workflow
    type and signals are referenced by name so this module stays free of
    imports that would create a bootstrap cycle.
    """

    def __init__(
        self,
        *,
        host: str,
        namespace: str,
        task_queue: str,
        workflow_timeout_seconds: int,
    ) -> None:
        self._host = host
        self._namespace = namespace
        self._task_queue = task_queue
        self._workflow_timeout = timedelta(seconds=workflow_timeout_seconds)
        self._client: Client | None = None
        self._lock = asyncio.Lock()

    async def _connect(self) -> Client:
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    self._client = await Client.connect(
                        self._host, namespace=self._namespace
                    )
        return self._client

    async def start_run(self, request: dict) -> None:
        client = await self._connect()
        await client.start_workflow(
            "RunSessionWorkflow",
            arg=request,
            id=str(request["run_id"]),
            task_queue=self._task_queue,
            execution_timeout=self._workflow_timeout,
        )

    async def submit_input(
        self,
        run_id: str,
        interaction_id: str,
        value: object,
        idempotency_key: str | None = None,
    ) -> None:
        client = await self._connect()
        handle = client.get_workflow_handle(run_id)
        await handle.signal(
            "submit_input",
            args=[interaction_id, value, idempotency_key],
        )

    async def submit_approval(
        self,
        run_id: str,
        approval_id: str,
        decision: str,
        idempotency_key: str | None = None,
    ) -> None:
        client = await self._connect()
        handle = client.get_workflow_handle(run_id)
        await handle.signal(
            "submit_approval",
            args=[approval_id, decision, idempotency_key],
        )

    async def cancel(self, run_id: str) -> None:
        client = await self._connect()
        handle = client.get_workflow_handle(run_id)
        await handle.signal("cancel_run")

    async def get_status(self, run_id: str) -> dict:
        client = await self._connect()
        handle = client.get_workflow_handle(run_id)
        return await handle.query("get_status")

    async def wait_for_run(
        self, run_id: str, timeout_seconds: float | None = None
    ) -> dict:
        """Await workflow completion and return its terminal state dict.

        Raises ``asyncio.TimeoutError`` when the workflow does not finish
        within ``timeout_seconds``; with ``None`` the workflow's own execution
        timeout applies. Workflow-level failures are returned as a
        ``{"status": "failed", ...}`` payload instead of being raised.
        """

        client = await self._connect()
        handle = client.get_workflow_handle(run_id)
        timeout = (
            timedelta(seconds=timeout_seconds) if timeout_seconds is not None else None
        )
        try:
            if timeout is None:
                return await handle.result()
            return await asyncio.wait_for(
                handle.result(), timeout=timeout.total_seconds()
            )
        except WorkflowFailureError as exc:
            return {"status": "failed", "error": str(exc)}

    async def close(self) -> None:
        if self._client is not None:
            service_client = getattr(self._client, "service_client", None)
            close = getattr(service_client, "close", None)
            if close is not None:
                await close()
            self._client = None

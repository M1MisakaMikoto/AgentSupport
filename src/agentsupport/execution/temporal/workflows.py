"""One durable workflow per AgentSupport run.

workflow_id == run_id.  Signals are the durable replacement for RunCommand
rows; ``wait_condition`` replaces PAUSED + checkpoint serialization; the
event history replaces the ExecutionJob state machine.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from .activities import execute_run, fail_run, resume_run, stop_runner

MAX_SEGMENT_TIMEOUT = timedelta(hours=6)
SEGMENT_HEARTBEAT_TIMEOUT = timedelta(minutes=2)
SEGMENT_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=1),
)


@workflow.defn
class RunSessionWorkflow:
    def __init__(self) -> None:
        self._request: dict | None = None
        self._input: dict | None = None
        self._approval: dict | None = None
        self._cancel_requested = False
        self._signal_keys: set[str] = set()
        self._status: str = "starting"

    @workflow.run
    async def run(self, request: dict) -> dict:
        self._request = request
        try:
            state = await self._run_segment(None)
            while state.get("status") == "waiting":
                # Durable pause at a human gate -- the replacement for PAUSED.
                self._status = "waiting"
                await workflow.wait_condition(
                    lambda: self._input is not None
                    or self._approval is not None
                    or self._cancel_requested
                )
                if self._cancel_requested:
                    await self._finish_cancelled(request)
                    return {"status": "cancelled"}
                if self._input is not None:
                    payload = {
                        "checkpoint_id": state.get("checkpoint_id"),
                        "input": self._input,
                    }
                    self._input = None
                else:
                    payload = {
                        "checkpoint_id": state.get("checkpoint_id"),
                        "approval": self._approval,
                    }
                    self._approval = None
                self._status = "running"
                state = await self._run_segment(payload)
            await workflow.execute_activity(
                stop_runner,
                {"request": request},
                start_to_close_timeout=timedelta(minutes=5),
            )
            self._status = state.get("state", state.get("status", "completed"))
            return state
        except ActivityError as exc:
            return await self._fail(request, exc)
        except Exception as exc:  # noqa: BLE001 - workflow-level safety net
            return await self._fail(request, exc)

    async def _fail(self, request: dict, exc: Exception) -> dict:
        """Persist ``run.failed`` so the conversation reaches a terminal state."""

        await workflow.execute_activity(
            fail_run,
            {
                "request": request,
                "error": {"code": "WORKFLOW_FAILED", "message": str(exc)},
            },
            start_to_close_timeout=timedelta(minutes=2),
        )
        self._status = "failed"
        return {"status": "failed", "error": str(exc)}

    async def _run_segment(self, payload: dict | None) -> dict:
        if payload is None:
            task = asyncio.create_task(
                workflow.execute_activity(
                    execute_run,
                    self._request,
                    start_to_close_timeout=MAX_SEGMENT_TIMEOUT,
                    heartbeat_timeout=SEGMENT_HEARTBEAT_TIMEOUT,
                    retry_policy=SEGMENT_RETRY,
                )
            )
        else:
            task = asyncio.create_task(
                workflow.execute_activity(
                    resume_run,
                    {"request": self._request, **payload},
                    start_to_close_timeout=MAX_SEGMENT_TIMEOUT,
                    heartbeat_timeout=SEGMENT_HEARTBEAT_TIMEOUT,
                    retry_policy=SEGMENT_RETRY,
                )
            )
        # Wait for the segment to finish or a cancel signal to arrive.
        while not task.done():
            await workflow.wait_condition(lambda: self._cancel_requested or task.done())
            if task.done():
                break
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, ActivityError):
                await task
            await self._finish_cancelled(self._request)
            return {"status": "cancelled"}
        return await task

    async def _finish_cancelled(self, request: dict) -> None:
        await workflow.execute_activity(
            stop_runner,
            {"request": request, "terminal": "cancelled"},
            start_to_close_timeout=timedelta(minutes=5),
        )

    @workflow.signal
    async def submit_input(
        self, interaction_id: str, value: Any, idempotency_key: str | None = None
    ) -> None:
        if idempotency_key is not None:
            if idempotency_key in self._signal_keys:
                return
            self._signal_keys.add(idempotency_key)
        self._input = {"interaction_id": interaction_id, "value": value}

    @workflow.signal
    async def submit_approval(
        self, approval_id: str, decision: str, idempotency_key: str | None = None
    ) -> None:
        if idempotency_key is not None:
            if idempotency_key in self._signal_keys:
                return
            self._signal_keys.add(idempotency_key)
        self._approval = {"approval_id": approval_id, "decision": decision}

    @workflow.signal
    async def cancel_run(self) -> None:
        self._cancel_requested = True

    @workflow.query
    def get_status(self) -> dict:
        return {"status": self._status}

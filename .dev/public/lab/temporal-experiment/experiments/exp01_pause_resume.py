"""Exp 01: pause-for-human + resume (the core checkpoint behaviour).

Current AgentSupport: on WAITING_INPUT it saves a Checkpoint, stops the
runner container, later claims an input command, restarts the container and
resumes from the checkpoint.

Temporal: the workflow simply waits on a signal.  The pending interaction is
workflow state (durable and queryable), no container/activity is alive while
waiting, and resume is just "signal the workflow".  This experiment also
proves the idempotency-key guard: replaying the same command does nothing.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from common import SideEffectStore, fresh_store



@activity.defn
async def start_agent_task(task: str, side_effect_path: str) -> str:
    SideEffectStore(side_effect_path).append("start_agent_task")
    await asyncio.sleep(0.3)
    return f"started:{task}"


@activity.defn
async def continue_agent_task(payload: dict, side_effect_path: str) -> str:
    SideEffectStore(side_effect_path).append("continue_agent_task")
    await asyncio.sleep(0.3)
    return f"finished:decision={payload['decision']}"


@workflow.defn
class PauseResumeWorkflow:
    def __init__(self) -> None:
        self._input: str | None = None
        self._pending: dict | None = None
        self._processed_keys: set[str] = set()

    @workflow.run
    async def run(self, task: str, side_effect_path: str) -> dict:
        await workflow.execute_activity(
            start_agent_task,
            args=[task, side_effect_path],
            start_to_close_timeout=timedelta(seconds=30),
        )
        # Reaching a human gate: the "checkpoint" is now just workflow state.
        self._pending = {
            "interaction_id": "interaction-1",
            "question": "approve edit to src/main.py?",
            "task": task,
        }
        # Durable wait.  No runner container is alive here -- the equivalent
        # of PAUSED, but with zero serialization/restart round-trips.
        await workflow.wait_condition(lambda: self._input is not None)
        result = await workflow.execute_activity(
            continue_agent_task,
            args=[{"decision": self._input}, side_effect_path],
            start_to_close_timeout=timedelta(seconds=30),
        )
        return {"result": result, "input": self._input}

    @workflow.signal
    async def submit_input(self, value: str, idempotency_key: str | None = None) -> None:
        # Mirrors RunCommand.idempotency_key: a replayed command is a no-op.
        if idempotency_key is not None:
            if idempotency_key in self._processed_keys:
                return
            self._processed_keys.add(idempotency_key)
        self._input = value

    @workflow.query
    def pending_interaction(self) -> dict | None:
        return self._pending


async def run(evidence_dir: Path) -> str:
    from common import TASK_QUEUE, connect
    from uuid import uuid4

    store_path = evidence_dir / "exp01_pause_resume.json"
    fresh_store(store_path)
    client = await connect()
    handle = await client.start_workflow(
        PauseResumeWorkflow.run,
        args=["fix the failing test", str(store_path)],
        id=f"exp01-pause-resume-{uuid4().hex[:8]}",
        task_queue=TASK_QUEUE,
    )

    # 1. wait until the workflow is parked at the human gate
    deadline = asyncio.get_running_loop().time() + 30
    pending = None
    while asyncio.get_running_loop().time() < deadline:
        pending = await handle.query(PauseResumeWorkflow.pending_interaction)
        if pending is not None:
            break
        await asyncio.sleep(0.2)
    assert pending is not None, "workflow never reached the human gate"

    # 2. while waiting, no activity is running (no runner alive)
    desc = await handle.describe()
    assert not desc.raw_description.pending_activities, (
        "an activity is running while paused"
    )

    # 3. deliver the decision, then replay the SAME command (idempotent)
    await handle.signal(PauseResumeWorkflow.submit_input, args=["approve", "key-1"])
    await handle.signal(PauseResumeWorkflow.submit_input, args=["deny", "key-1"])

    result = await handle.result()
    assert result["input"] == "approve", f"idempotency failed: {result}"
    assert result["result"] == "finished:decision=approve"

    store = SideEffectStore(store_path)
    assert store.count("continue_agent_task") == 1, (
        "continue_agent_task ran more than once despite the duplicate signal"
    )
    return (
        "pause-for-human is workflow state (queryable, no container alive); "
        "resume via signal; duplicate command with same key is a no-op"
    )

WORKFLOWS = [PauseResumeWorkflow]
ACTIVITIES = [start_agent_task, continue_agent_task]

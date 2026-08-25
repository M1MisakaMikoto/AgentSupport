"""Exp 04: crash recovery without re-executing completed work.

Current AgentSupport: any crash while RUNNING retries the WHOLE task from
scratch (the checkpoint is only saved at human-gate pauses).

Temporal: the event history survives worker death.  Only the activity that
was in flight is retried; completed activities are never re-executed.

Setup: a dedicated worker subprocess runs the workflow.  We start it, wait
until the second activity (step_b, long and heartbeating) is running, then
hard-kill the worker, wait for the heartbeat timeout, restart the worker and
let the workflow finish.

Evidence (side-effect store):
  step_a        == 1   (completed before the crash, NOT re-run)
  step_b        == 2   (started, killed, retried)
  step_b_done   == 1   (retried attempt finished)
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

from common import SideEffectStore, TASK_QUEUE, connect, fresh_store
from crash_defs import CrashRecoveryWorkflow

WORKER_PROC = Path(__file__).resolve().parent / "worker_proc.py"
WORKFLOWS: list = []
ACTIVITIES: list = []


def _spawn_worker():
    return subprocess.Popen(
        [sys.executable, str(WORKER_PROC)],
        cwd=str(WORKER_PROC.parent),
    )


async def _wait_for(store_path: Path, name: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    store = SideEffectStore(store_path)
    while time.monotonic() < deadline:
        if store.count(name) >= 1:
            return
        await asyncio.sleep(0.2)
    raise AssertionError(f"never observed '{name}' in side-effect store")


async def run(evidence_dir: Path) -> str:
    store_path = evidence_dir / "exp04_crash_recovery.json"
    from uuid import uuid4

    fresh_store(store_path)
    client = await connect()
    handle = await client.start_workflow(
        CrashRecoveryWorkflow.run,
        str(store_path),
        id=f"exp04-crash-recovery-{uuid4().hex[:8]}",
        task_queue=TASK_QUEUE,
    )

    proc = _spawn_worker()
    try:
        # 1. wait until the long activity is actually executing
        await _wait_for(store_path, "step_b")
        # 2. hard-kill the worker mid-flight
        proc.kill()
        proc.wait()
        # 3. let the heartbeat timeout (3s) mark the activity failed
        await asyncio.sleep(5)
        # 4. a fresh worker takes over; workflow resumes from history
        proc = _spawn_worker()
        result = await handle.result()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    assert result == "step-a-done+step-b-done", f"unexpected result: {result}"
    store = SideEffectStore(store_path)
    assert store.count("step_a") == 1, "completed step_a was re-executed after crash"
    assert store.count("step_b") == 2, f"step_b retries != 2: {store.count('step_b')}"
    assert store.count("step_b_done") == 1, "retried step_b never finished"
    return (
        "worker killed mid-run; only the in-flight activity was retried "
        "(step_a ran once, step_b twice); workflow completed from history"
    )

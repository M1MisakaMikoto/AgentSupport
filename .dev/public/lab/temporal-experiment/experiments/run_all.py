"""Run every lab experiment and report PASS/FAIL per checkpoint feature."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from temporalio.client import Client  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from common import NAMESPACE, TASK_QUEUE, address, connect  # noqa: E402

import exp01_pause_resume as exp01  # noqa: E402
import exp02_cancel as exp02  # noqa: E402
import exp03_idempotent_signals as exp03  # noqa: E402
import exp04_crash_recovery as exp04  # noqa: E402
import exp05_checkpoint_combined as exp05  # noqa: E402
import exp06_heartbeat_timeout as exp06  # noqa: E402

EXPERIMENTS = [
    ("exp01_pause_resume", exp01),
    ("exp02_cancel", exp02),
    ("exp03_idempotent_signals", exp03),
    ("exp05_checkpoint_combined", exp05),
    ("exp06_heartbeat_timeout", exp06),
]


def _all_workflows():
    workflows = []
    for _name, mod in EXPERIMENTS:
        workflows.extend(getattr(mod, "WORKFLOWS"))
    return workflows


def _all_activities():
    activities = []
    for _name, mod in EXPERIMENTS:
        activities.extend(getattr(mod, "ACTIVITIES"))
    return activities


async def main() -> int:
    evidence_dir = Path(__file__).resolve().parent / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    results: list[tuple[str, bool, str]] = []

    async def run_one(name: str, mod) -> None:
        try:
            detail = await mod.run(evidence_dir)
            results.append((name, True, detail))
            print(f"[PASS] {name}: {detail}")
        except Exception as exc:  # noqa: BLE001 - report every experiment
            results.append((name, False, repr(exc)))
            print(f"[FAIL] {name}: {exc!r}")

    # exp04 owns its worker subprocess (it kills and restarts it).
    await run_one("exp04_crash_recovery", exp04)

    # The remaining experiments share one in-process worker.
    client: Client = await connect()
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=_all_workflows(),
        activities=_all_activities(),
    )
    worker_task = asyncio.create_task(worker.run())
    try:
        for name, mod in EXPERIMENTS:
            await run_one(name, mod)
    finally:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

    print()
    failed = [name for name, ok, _ in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} experiments passed")
    if failed:
        print("failed:", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

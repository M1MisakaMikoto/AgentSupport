"""Experiment 3: fire-and-forget event publish tasks.

``AgentSupportService._append`` schedules ``events_store.publish`` with an
untracked ``asyncio.create_task``. This experiment shows (a) publishes pile up
with no handle to observe or bound them and (b) a failing publish surfaces as
an unhandled "Task exception was never retrieved" instead of a logged error.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import logging
import tempfile
from pathlib import Path
from uuid import uuid4

from common import record_evidence

from agentsupport.adapters.notification import InMemoryEventNotifier, InMemoryEventStore
from agentsupport.adapters.runtime import DockerRuntimeDriver
from agentsupport.adapters.skills import LocalSkillProvider
from agentsupport.adapters.workspace import LocalWorkspaceStorageDriver
from agentsupport.application.runner_registry import InMemoryRunnerRegistry
from agentsupport.application.service import AgentSupportService
from agentsupport.bootstrap.settings import Settings
from agentsupport.domain import Conversation


class ProbeStore(InMemoryEventStore):
    def __init__(self, *, fail: bool = False, delay: float = 0.0) -> None:
        super().__init__()
        self.fail = fail
        self.delay = delay
        self.inflight: set[asyncio.Task] = set()
        self.calls = 0

    async def publish(self, conversation_id, event) -> None:
        current = asyncio.current_task()
        self.inflight.add(current)
        self.calls += 1
        if self.fail:
            self.inflight.discard(current)
            raise RuntimeError("probe publish failure")
        if self.delay:
            await asyncio.sleep(self.delay)
        self.inflight.discard(current)


def _build_service(store: ProbeStore) -> AgentSupportService:
    config = Settings(
        persistence_mode="memory",
        execution_mode="inline",
        database_url="",
        auto_create_schema=False,
    )
    with tempfile.TemporaryDirectory(prefix="exp3-ws-") as tmp:
        workspace_root = Path(tmp)
    workspace_root.mkdir(parents=True, exist_ok=True)
    skills_root = workspace_root / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    return AgentSupportService(
        config,
        events_store=store,
        event_notifier=InMemoryEventNotifier(),
        workspace_provider=LocalWorkspaceStorageDriver(workspace_root),
        skill_provider=LocalSkillProvider(skills_root),
        runtime_driver=DockerRuntimeDriver(),
        core_runtime=None,
        repository=None,
        runner_registry=InMemoryRunnerRegistry(),
    )


def main(phase: str) -> None:
    async def run() -> str:
        sections: list[str] = []

        # --- (a) pile-up with no tracking/backpressure ---------------------
        slow = ProbeStore(delay=0.05)
        service = _build_service(slow)
        conversation = Conversation(session_id=uuid4(), task="exp3")
        for i in range(30):
            service._append(conversation, "message", {"i": i})
        queued_immediately = len(asyncio.all_tasks()) - 1  # minus the main task
        await asyncio.sleep(0.02)
        inflight_at_20ms = len(slow.inflight)
        calls_at_20ms = slow.calls
        tracked = getattr(service, "_publish_tasks", None)
        sections.append(
            "### (a) slow publish, 30 appends\n\n"
            f"- publish calls at +20ms: {calls_at_20ms}/30\n"
            f"- publish tasks still in flight at +20ms: {inflight_at_20ms}\n"
            f"- asyncio tasks created by the event loop at +0ms: {queued_immediately}\n"
            f"- tracked publish tasks on the service: "
            f"{'<missing attribute _publish_tasks>' if tracked is None else len(tracked)}\n"
        )
        await asyncio.sleep(1.0)  # let all publishes finish
        tracked_after = getattr(service, "_publish_tasks", None)
        sections.append(
            "### (a.2) after all publishes finish\n\n"
            f"- tracked publish tasks on the service: "
            f"{'<missing attribute _publish_tasks>' if tracked_after is None else len(tracked_after)}\n"
        )

        # --- (b) failing publish: captured logs ---------------------------
        records: list[logging.LogRecord] = []

        class CaptureHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        root = logging.getLogger()
        capture = CaptureHandler(level=logging.ERROR)
        root.addHandler(capture)
        try:
            failing = ProbeStore(fail=True)
            service2 = _build_service(failing)
            conversation2 = Conversation(session_id=uuid4(), task="exp3-fail")
            service2._append(conversation2, "message", {"i": 1})
            await asyncio.sleep(0.05)
            gc.collect()
            await asyncio.sleep(0.05)
        finally:
            root.removeHandler(capture)

        messages = [r.getMessage() for r in records if "never retrieved" in r.getMessage().lower()]
        app_errors = [
            r.getMessage()
            for r in records
            if "publish" in r.getMessage().lower() or "traceback" in r.getMessage().lower()
        ]
        sections.append(
            "### (b) failing publish\n\n"
            f"- asyncio 'Task exception was never retrieved' records: {len(messages)}\n"
            f"  ```\n" + "\n".join(messages[:2]) + "\n  ```\n"
            f"- application-level error records: {len(app_errors)}\n"
            f"  ```\n" + "\n".join(app_errors[:2]) + "\n  ```\n"
        )
        if phase == "after":
            tracked = getattr(service2, "_publish_tasks", None)
            assert tracked is not None, "publish tasks are not tracked"
            assert len(messages) == 0, "unhandled asyncio task exceptions remain"
            assert len(app_errors) >= 1, "publish failure was not logged"
        return "\n\n".join(sections)

    markdown = asyncio.run(run())
    record_evidence(
        "exp3_publish_tasks",
        phase,
        markdown,
        command=f"python devtools/experiments/exp3_publish_tasks.py --phase {phase}",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=["before", "after"])
    args = parser.parse_args()
    main(args.phase)

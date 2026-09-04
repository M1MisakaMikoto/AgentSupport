"""Test-only substitutes for infrastructure the core no longer ships.

The product core runs on Temporal with a resident runner pool. These helpers
let the unit/contract suite exercise the same service -> workflow-like
activity -> repository chain without a Temporal server or a real model:

- ``EmbeddedTemporalCoordinator`` replays ``RunSessionWorkflow`` semantics by
  driving the real activities in-process;
- ``FakeRunner`` plays the Session Runner HTTP contract without an LLM.

Nothing in this module is imported by ``src/``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID

from agent_runner_contracts.events import EventEnvelope

from agentsupport.adapters.notification import InMemoryEventStore, create_event_notifier
from agentsupport.adapters.persistence.sqlalchemy.repository import PostgresRepository
from agentsupport.adapters.registry import SqlAlchemyRunnerRegistry
from agentsupport.adapters.skills.local import LocalSkillProvider
from agentsupport.adapters.workspace import LocalWorkspaceProvider
from agentsupport.application.service import AgentSupportService
from agentsupport.bootstrap.settings import Settings
from agentsupport.domain import Checkpoint, ContextBundle
from agentsupport.execution.temporal import activities as temporal_activities
from agentsupport.execution.temporal.activities import (
    ExecutionContext,
    execute_run,
    fail_run,
    resume_run,
    set_execution_context,
    stop_runner,
)


def _noop_heartbeat(*_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
    return None


class FakeRunner:
    """Simulates the Session Runner HTTP contract used by the activities."""

    def __init__(self, *, gate: bool = False, result: dict | None = None) -> None:
        self.gate = gate
        self.result = result
        self.captured: list[dict] = []
        self.resume_calls = 0

    def _interaction(self, run_id: UUID) -> dict:
        return {
            "interaction_id": "interaction-1",
            "kind": "approval",
            "tool_batch_hash": "batch-1",
            "tool_batch": {"calls": []},
            "pending_tool_calls": [],
            "tool_policy": {
                "allowed_tools": ["bash"],
                "approval_required_tools": ["bash"],
            },
        }

    async def run(self, request: dict, event_sink) -> dict:
        self.captured.append(request)
        run_id = UUID(request["run_id"])
        await event_sink(
            EventEnvelope(
                run_id=run_id,
                seq=0,
                type="run.started",
                payload={"conversation_id": request["conversation_id"]},
                source="runner",
            )
        )
        await event_sink(
            EventEnvelope(
                run_id=run_id, seq=0, type="run.running", payload={}, source="runner"
            )
        )
        if self.gate:
            await event_sink(
                EventEnvelope(
                    run_id=run_id,
                    seq=0,
                    type="interaction.requested",
                    payload=self._interaction(run_id),
                    source="runner",
                )
            )
            return {"status": "waiting"}
        await event_sink(
            EventEnvelope(
                run_id=run_id,
                seq=0,
                type="run.completed",
                payload={
                    "result": self.result
                    or {"status": "completed", "content": "done"}
                },
                source="runner",
            )
        )
        return {"status": "completed"}

    async def checkpoint(self, run_id, reason):
        if not isinstance(run_id, UUID):
            run_id = UUID(run_id)
        return Checkpoint(
            conversation_id=UUID("00000000-0000-0000-0000-000000000000"),
            run_id=run_id,
            last_event_seq=0,
            context_bundle=ContextBundle(
                task="write a test",
                conversation_id=UUID("00000000-0000-0000-0000-000000000000"),
                workspace_ref="/workspace-data/session",
                recent_events=[],
                tool_policy={"allowed_tools": ["bash"]},
            ),
            pending_interaction=self._interaction(run_id),
            tool_batch_hash="batch-1",
            context_bundle_hash="test",
            workspace_ref="/workspace-data/session",
            workspace_write_lease_epoch=0,
        )

    async def resume(self, checkpoint, value, event_sink, *, command_id=None, runtime_context=None) -> dict:
        self.resume_calls += 1
        await event_sink(
            EventEnvelope(
                run_id=checkpoint.run_id,
                seq=0,
                type="run.completed",
                payload={
                    "result": self.result
                    or {"status": "completed", "content": "done"}
                },
                source="runner",
            )
        )
        return {"status": "completed"}

    async def accept_input(self, run_id, interaction_id, value, *, command_id=None):
        return {}

    async def accept_approval(self, run_id, approval_id, decision, *, command_id=None):
        return {}

    async def cancel(self, run_id, *, command_id=None):
        return {}


class EmbeddedTemporalCoordinator:
    """Runs the workflow semantics in-process without a Temporal server."""

    def __init__(self, context: ExecutionContext) -> None:
        self._ctx = context
        self._tasks: dict[str, asyncio.Task] = {}
        self._waiting: dict[str, asyncio.Event] = {}
        self._decision: dict[str, dict | None] = {}
        self._status: dict[str, str] = {}

    async def start_run(self, request: dict) -> None:
        run_id = str(request["run_id"])
        self._status[run_id] = "starting"
        self._tasks[run_id] = asyncio.create_task(self._drive(request, run_id))

    async def _drive(self, request: dict, run_id: str) -> dict:
        set_execution_context(self._ctx)
        temporal_activities.activity.heartbeat = _noop_heartbeat  # type: ignore[assignment]
        try:
            state = await execute_run(request)
            while state.get("status") == "waiting":
                self._status[run_id] = "waiting"
                event = self._waiting.setdefault(run_id, asyncio.Event())
                event.clear()
                await event.wait()
                decision = self._decision.pop(run_id, None)
                if decision is None:
                    await stop_runner({"request": request, "terminal": "cancelled"})
                    self._status[run_id] = "cancelled"
                    return {"status": "cancelled"}
                self._status[run_id] = "running"
                payload = {"request": request, "checkpoint_id": state.get("checkpoint_id")}
                payload.update(decision)
                state = await resume_run(payload)
            self._status[run_id] = "completed"
            await stop_runner({"request": request})
            return state
        except Exception as exc:  # noqa: BLE001 - embedded safety net
            self._status[run_id] = "failed"
            try:
                await fail_run(
                    {
                        "request": request,
                        "error": {"code": "EMBEDDED_FAILED", "message": str(exc)},
                    }
                )
            except Exception:  # noqa: BLE001 - best effort
                pass
            return {"status": "failed", "error": str(exc)}

    def _wake(self, run_id: str) -> None:
        event = self._waiting.get(run_id)
        if event is not None and not event.is_set():
            event.set()

    async def submit_input(
        self,
        run_id: str,
        interaction_id: str,
        value: object,
        idempotency_key: str | None = None,
    ) -> None:
        self._decision[str(run_id)] = {
            "input": {
                "interaction_id": interaction_id,
                "value": value,
                "idempotency_key": idempotency_key,
            }
        }
        self._wake(str(run_id))

    async def submit_approval(
        self,
        run_id: str,
        approval_id: str,
        decision: str,
        idempotency_key: str | None = None,
    ) -> None:
        self._decision[str(run_id)] = {
            "approval": {
                "approval_id": approval_id,
                "decision": decision,
                "idempotency_key": idempotency_key,
            }
        }
        self._wake(str(run_id))

    async def cancel(self, run_id: str) -> None:
        self._decision[str(run_id)] = None
        self._wake(str(run_id))

    async def get_status(self, run_id: str) -> dict:
        return {"status": self._status.get(str(run_id), "starting")}

    async def wait_for_run(
        self, run_id: str, timeout_seconds: float | None = None
    ) -> dict:
        task = self._tasks[str(run_id)]
        if timeout_seconds is None:
            return await task
        return await asyncio.wait_for(task, timeout=timeout_seconds)

    async def close(self) -> None:
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        self._tasks.clear()


def make_temporal_service(
    tmp_path,
    *,
    gate: bool = False,
    runner_result: dict | None = None,
    workspace_provider=None,
    **overrides,
) -> SimpleNamespace:
    """Build a service wired to the embedded coordinator and a fake runner."""

    settings_kwargs = {
        "persistence_mode": "postgres",
        "execution_mode": "temporal",
        "database_url": f"sqlite:///{tmp_path / 'temporal.db'}",
        "auto_create_schema": True,
        "core_runner_url": "http://runner",
        "workspace_root": tmp_path / "workspace",
        "skills_root": tmp_path / "skills",
    }
    settings_kwargs.update(overrides)
    config = Settings(**settings_kwargs)
    repository = PostgresRepository(config.database_url, create_schema=True)
    fake = FakeRunner(gate=gate, result=runner_result)
    skills_root = config.skills_root
    skills_root.mkdir(parents=True, exist_ok=True)
    workspace_root = config.workspace_root
    workspace_root.mkdir(parents=True, exist_ok=True)
    context = ExecutionContext(
        config=config,
        repository=repository,
        core_runtime=fake,
    )
    context.skill_provider = LocalSkillProvider(skills_root)
    set_execution_context(context)
    coordinator = EmbeddedTemporalCoordinator(context)
    service = AgentSupportService(
        config,
        events_store=InMemoryEventStore(),
        event_notifier=create_event_notifier(None),
        workspace_provider=workspace_provider or LocalWorkspaceProvider(workspace_root),
        skill_provider=LocalSkillProvider(skills_root),
        core_runtime=fake,
        repository=repository,
        temporal=coordinator,
        runner_registry=SqlAlchemyRunnerRegistry(repository),
    )
    # The control plane never talks to a runner directly in temporal mode;
    # the fake lives in the activity context instead. Diagnostics use
    # ``service.core_runtime`` as the "runner configured" signal.
    service.core_runtime = None
    return SimpleNamespace(
        config=config,
        service=service,
        repository=repository,
        fake=fake,
        coordinator=coordinator,
    )

"""Phase 1 gate: old and new execution layers agree on the same task.

Both layers drive the SAME real deterministic session runner (in-process
ASGI) for the same task and approval flow.  The assertion is the milestone
contract of the runner-driven events: interaction.requested -> approval.
decided -> run.completed, in order, with the same terminal state.  Plumbing
events (e.g. service-appended run.started/run.running) are layer-specific and
deliberately not compared.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from httpx import ASGITransport

from agentsupport.adapters.notification import InMemoryEventStore, create_event_notifier
from agentsupport.adapters.persistence.sqlalchemy.repository import PostgresRepository
from agentsupport.adapters.skills.local import LocalSkillProvider
from agentsupport.adapters.workspace import LocalWorkspaceProvider
from agentsupport.application.service import AgentSupportService
from agentsupport.bootstrap.settings import Settings
from agentsupport.core_runtime import TraeCoreRunnerRuntime
from agentsupport.domain import ExecutionState
from agentsupport.execution.temporal.activities import (
    ExecutionContext,
    execute_run,
    fail_run,
    resume_run,
    set_execution_context,
    start_runner,
    stop_runner,
)
from agentsupport.execution.temporal.client import TemporalRunCoordinator
from agentsupport.execution.temporal.workflows import RunSessionWorkflow
from agentsupport.services import AgentSupportService as FacadeService
from session_runner.server import create_runner_app

MILESTONES = ("interaction.requested", "approval.decided", "run.completed")


async def _old_layer(tmp_path, task: str) -> list[str]:
    service = FacadeService(Settings(workspace_root=tmp_path / "ws-inline"))
    service.core_runtime = TraeCoreRunnerRuntime(
        "http://runner", transport=ASGITransport(app=create_runner_app())
    )
    workspace = service.create_workspace("parity")
    session = service.create_session(workspace.id)
    conversation = await service.create_conversation(session.id, task)
    approval_id = conversation.run.pending_interaction["interaction_id"]
    conversation = await service.submit_approval(
        conversation.id, approval_id, "APPROVE_ONCE"
    )
    assert conversation.run.state == ExecutionState.COMPLETED
    return [event.type for event in service.events(conversation.id)]


async def _new_layer(tmp_path, task: str) -> tuple[list[str], ExecutionState]:
    from temporalio.client import Client
    from temporalio.worker import Worker

    try:
        client = await Client.connect("localhost:7233", namespace="default")
    except Exception as exc:  # noqa: BLE001 - environment gate
        pytest.skip(f"Temporal dev server not reachable: {exc}")

    config = Settings(
        persistence_mode="postgres",
        execution_mode="temporal",
        database_url=f"sqlite:///{tmp_path / 'parity.db'}",
        auto_create_schema=True,
        temporal_host="localhost:7233",
        temporal_namespace="default",
        temporal_task_queue="agentsupport-parity",
        core_runner_url="http://runner",
        workspace_root=tmp_path / "ws-temporal",
        skills_root=tmp_path / "skills",
    )
    repository = PostgresRepository(config.database_url, create_schema=True)
    skills_root = tmp_path / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    runner = TraeCoreRunnerRuntime(
        "http://runner", transport=ASGITransport(app=create_runner_app())
    )
    context = ExecutionContext(config=config, repository=repository, core_runtime=runner)
    context.skill_provider = LocalSkillProvider(skills_root)
    set_execution_context(context)
    service = AgentSupportService(
        config,
        events_store=InMemoryEventStore(),
        event_notifier=create_event_notifier(None),
        workspace_provider=LocalWorkspaceProvider(tmp_path / "ws-temporal"),
        skill_provider=LocalSkillProvider(skills_root),
        runtime_driver=context.runtime_driver,
        core_runtime=runner,
        repository=repository,
        temporal=TemporalRunCoordinator(
            host=config.temporal_host,
            namespace=config.temporal_namespace,
            task_queue=config.temporal_task_queue,
            workflow_timeout_seconds=config.temporal_workflow_timeout_seconds,
        ),
    )
    worker = Worker(
        client,
        task_queue=config.temporal_task_queue,
        workflows=[RunSessionWorkflow],
        activities=[start_runner, execute_run, resume_run, stop_runner, fail_run],
    )
    worker_task = asyncio.create_task(worker.run())
    try:
        workspace = service.create_workspace("parity")
        session = service.create_session(workspace.id)
        conversation = await service.create_conversation(session.id, task)
        conversation_id = conversation.id
        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            persisted = repository.get_conversation(conversation_id)
            if (
                persisted is not None
                and persisted.run.state == ExecutionState.WAITING_INPUT
                and persisted.run.pending_interaction is not None
            ):
                break
            await asyncio.sleep(0.1)
        persisted = repository.get_conversation(conversation_id)
        assert persisted is not None
        approval_id = persisted.run.pending_interaction["interaction_id"]
        await service.submit_approval(conversation.id, approval_id, "APPROVE_ONCE")
        while asyncio.get_running_loop().time() < deadline:
            persisted = repository.get_conversation(conversation_id)
            if persisted is not None and persisted.run.state == ExecutionState.COMPLETED:
                break
            await asyncio.sleep(0.1)
        persisted = repository.get_conversation(conversation_id)
        assert persisted is not None
        assert persisted.run.state == ExecutionState.COMPLETED
        return (
            [event.type for event in repository.list_events(conversation_id)],
            persisted.run.state,
        )
    finally:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        await service.temporal.close()


@pytest.mark.asyncio
async def test_dual_run_parity_same_milestones(tmp_path):
    task = "ask: approve tool?"
    old_types = await _old_layer(tmp_path, task)
    new_types, new_state = await _new_layer(tmp_path, task)

    old_milestones = [t for t in old_types if t in MILESTONES]
    new_milestones = [t for t in new_types if t in MILESTONES]

    assert new_state == ExecutionState.COMPLETED
    assert old_milestones == list(MILESTONES), f"old layer: {old_milestones}"
    assert new_milestones == list(MILESTONES), f"new layer: {new_milestones}"
    assert old_milestones == new_milestones

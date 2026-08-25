"""Temporal-mode evaluation integration tests.

These tests need a reachable Temporal dev server (default localhost:7233) and
are skipped otherwise so the regular suite stays green.  Start one with::

    temporal server start-dev --headless --port 7233

The eval layer drives the real platform execution path in
``execution_mode=temporal``; the deterministic in-process Session Runner plays
the role of the agent so the chain service -> workflow -> activity ->
repository -> eval verdict is exercised without a real model.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from httpx import ASGITransport

from agentsupport.adapters.notification import InMemoryEventStore, create_event_notifier
from agentsupport.adapters.persistence.sqlalchemy.eval_store import SqlAlchemyEvalStore
from agentsupport.adapters.persistence.sqlalchemy.repository import PostgresRepository
from agentsupport.adapters.runner import TraeCoreRunnerRuntime
from agentsupport.adapters.skills.local import LocalSkillProvider
from agentsupport.adapters.workspace import LocalWorkspaceProvider
from agentsupport.adapters.workspace.providers import LocalWorkspaceStorageDriver
from agentsupport.application.service import AgentSupportService
from agentsupport.bootstrap.settings import Settings
from agentsupport.evaluation import EvalService, VerifierConfig
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
from session_runner.server import create_runner_app


class ExplodingCoreRuntime:
    """Runner that always fails; drives the workflow failure path."""

    async def run(self, request: dict, event_sink) -> None:
        raise RuntimeError("runner exploded")


class BlockingCoreRuntime:
    """Runner that never returns; drives the workflow execution-timeout path."""

    async def run(self, request: dict, event_sink) -> None:
        await asyncio.Event().wait()


@pytest.fixture
async def env(tmp_path):
    from temporalio.client import Client
    from temporalio.worker import Worker

    try:
        client = await Client.connect("localhost:7233", namespace="default")
    except Exception as exc:  # noqa: BLE001 - environment gate
        pytest.skip(f"Temporal dev server not reachable: {exc}")

    config = Settings(
        persistence_mode="postgres",
        execution_mode="temporal",
        database_url=f"sqlite:///{tmp_path / 'temporal-eval.db'}",
        auto_create_schema=True,
        temporal_host="localhost:7233",
        temporal_namespace="default",
        temporal_task_queue="agentsupport-eval-test",
        core_runner_url="http://runner",
        workspace_root=tmp_path / "workspace",
        skills_root=tmp_path / "skills",
        eval_case_timeout_seconds=60,
    )
    repository = PostgresRepository(config.database_url, create_schema=True)
    skills_root = tmp_path / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    runner = TraeCoreRunnerRuntime(
        config.core_runner_url or "http://runner",
        timeout_seconds=config.core_runner_timeout_seconds,
        transport=ASGITransport(app=create_runner_app(runner_mode="deterministic")),
    )
    context = ExecutionContext(
        config=config,
        repository=repository,
        core_runtime=runner,
    )
    context.skill_provider = LocalSkillProvider(skills_root)
    set_execution_context(context)

    service = AgentSupportService(
        config,
        events_store=InMemoryEventStore(),
        event_notifier=create_event_notifier(None),
        workspace_provider=LocalWorkspaceProvider(tmp_path / "workspace"),
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
    eval_service = EvalService(
        workspace_driver=LocalWorkspaceStorageDriver(config.workspace_root),
        agentsupport=service,
        store=SqlAlchemyEvalStore(
            config.database_url,
            create_schema=config.auto_create_schema,
            engine=repository.engine,
        ),
        case_timeout_seconds=config.eval_case_timeout_seconds,
    )
    try:
        yield SimpleNamespace(
            config=config,
            service=service,
            eval_service=eval_service,
            repository=repository,
            context=context,
        )
    finally:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        await service.temporal.close()


def _seed(env) -> tuple[UUID, str]:
    workspace_id, path = env.eval_service.workspace_driver.create("src")
    (Path(path) / "check.py").write_text("import sys; sys.exit(0)", encoding="utf-8")
    version_id = env.eval_service.workspace_driver.create_version(workspace_id)
    return workspace_id, version_id


def _dataset(env, name: str, workspace_id: UUID, version_id: str):
    dataset = env.eval_service.create_dataset(
        name=name,
        workspace_id=workspace_id,
        baseline_version=version_id,
    )
    env.eval_service.add_case(
        dataset.id,
        task="make check.py pass",
        verifiers=[
            VerifierConfig(type="terminal_state", params={"expect": "COMPLETED"})
        ],
    )
    return dataset


@pytest.mark.asyncio
async def test_temporal_eval_completes_with_terminal_verdict(env):
    workspace_id, version_id = _seed(env)
    dataset = _dataset(env, "temporal-good", workspace_id, version_id)

    run = await env.eval_service.run_dataset(dataset.id)

    assert run.status == "completed"
    assert run.results[0].verdict == "PASS"
    assert run.results[0].outcome is not None
    assert run.results[0].outcome.terminal_state == "COMPLETED"


@pytest.mark.asyncio
async def test_temporal_eval_workflow_failure_is_failed_not_stuck(env):
    workspace_id, version_id = _seed(env)
    dataset = _dataset(env, "temporal-failure", workspace_id, version_id)

    env.context.core_runtime = ExplodingCoreRuntime()

    run = await env.eval_service.run_dataset(dataset.id)

    assert run.status == "completed"  # per-case failure, run finishes
    result = run.results[0]
    assert result.verdict == "FAIL"  # terminal_state verifier: FAILED != COMPLETED
    assert result.outcome is not None
    assert result.outcome.terminal_state == "FAILED"
    assert result.outcome.error is not None
    assert run.summary["failed"] == 1


@pytest.mark.asyncio
async def test_temporal_eval_run_idempotent_replay(env):
    workspace_id, version_id = _seed(env)
    dataset = _dataset(env, "temporal-replay", workspace_id, version_id)

    run = await env.eval_service.run_dataset(dataset.id, idempotency_key="run-replay")
    assert run.status == "completed"

    replay = await env.eval_service.run_dataset(dataset.id, idempotency_key="run-replay")

    assert replay.id == run.id
    assert replay.status == "completed"


@pytest.mark.asyncio
async def test_temporal_eval_workflow_execution_timeout_is_error_not_stuck(env):
    """A workflow terminated by its execution timeout must not leave a stuck case.

    The workflow is force-terminated at the execution timeout, so the ``_fail``
    activity never runs and the conversation stays non-terminal; evaluation
    must record the case as ERROR instead of judging a half-finished outcome.
    """

    workspace_id, version_id = _seed(env)
    dataset = _dataset(env, "temporal-hard-timeout", workspace_id, version_id)

    env.context.core_runtime = BlockingCoreRuntime()
    timeout_coordinator = TemporalRunCoordinator(
        host=env.config.temporal_host,
        namespace=env.config.temporal_namespace,
        task_queue=env.config.temporal_task_queue,
        workflow_timeout_seconds=5,
    )
    env.service.temporal = timeout_coordinator
    try:
        run = await env.eval_service.run_dataset(dataset.id)
    finally:
        await timeout_coordinator.close()

    assert run.status == "completed"  # per-case failure, run finishes
    result = run.results[0]
    assert result.verdict == "ERROR"
    assert "terminal conversation state" in result.verifier_results[0].reason

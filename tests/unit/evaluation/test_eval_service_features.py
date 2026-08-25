"""EvalService features: concurrency, cleanup, case-id compare, persisted idempotency."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from agentsupport.adapters.persistence.sqlalchemy.eval_store import SqlAlchemyEvalStore
from agentsupport.adapters.workspace.providers import LocalWorkspaceStorageDriver
from agentsupport.application.service import AgentSupportService, ServiceError
from agentsupport.bootstrap.container import build_agentsupport_dependencies
from agentsupport.bootstrap.settings import Settings
from agentsupport.evaluation.domain import EvalCaseResult, EvalDataset, EvalRun
from agentsupport.evaluation.service import EvalService
from agentsupport.evaluation.store import InMemoryEvalStore


class FakeWorkspaceDriver:
    def create_from_version(self, workspace_id: UUID, version_id: str) -> tuple[UUID, str]:
        return uuid4(), "/tmp/eval-ws"

    def list_versions(self, workspace_id: UUID) -> list[dict[str, object]]:
        return [{"version_id": "v1"}]


class FakeAgentSupport:
    repository = None

    def create_session(self, workspace_id: UUID, idempotency_key: str | None = None, **kwargs):
        return SimpleNamespace(id=uuid4())

    async def create_conversation(self, session_id: UUID, task: str):
        raise RuntimeError("unused")


def _service(store: InMemoryEvalStore | None = None, **kwargs) -> EvalService:
    return EvalService(
        workspace_driver=FakeWorkspaceDriver(),
        agentsupport=FakeAgentSupport(),  # type: ignore[arg-type]
        store=store or InMemoryEvalStore(),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_cases_run_with_bounded_concurrency():
    service = _service(case_concurrency=2)
    dataset = service.create_dataset(name="concurrent", workspace_id=uuid4(), baseline_version="v1")
    for index in range(4):
        service.add_case(dataset.id, task=f"task-{index}")

    active = 0
    max_active = 0
    lock = asyncio.Lock()

    async def fake_run_case(case, dataset):
        nonlocal active, max_active
        async with lock:
            active += 1
            max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        async with lock:
            active -= 1
        return EvalCaseResult(case_id=case.id, task=case.task, verdict="PASS", score=1.0)

    service._run_case = fake_run_case  # type: ignore[method-assign]
    run = await service.run_dataset(dataset.id)

    assert run.status == "completed"
    assert len(run.results) == 4
    assert max_active >= 2  # cases actually ran in parallel
    assert max_active <= 2  # bounded by the semaphore


@pytest.mark.asyncio
async def test_case_workspace_clone_removed_on_failure(tmp_path):
    driver = LocalWorkspaceStorageDriver(tmp_path)
    workspace_id, path = driver.create("src")
    (Path(path) / "check.py").write_text("import sys; sys.exit(0)", encoding="utf-8")
    version_id = driver.create_version(workspace_id)

    class FailingAgentSupport:
        repository = None

        def create_session(self, workspace_id: UUID, idempotency_key: str | None = None, **kwargs):
            raise RuntimeError("session creation failed")

    service = EvalService(
        workspace_driver=driver,
        agentsupport=FailingAgentSupport(),  # type: ignore[arg-type]
        store=InMemoryEvalStore(),
    )
    dataset = service.create_dataset(
        name="cleanup", workspace_id=workspace_id, baseline_version=version_id
    )
    service.add_case(dataset.id, task="task")

    run = await service.run_dataset(dataset.id)

    assert run.results[0].verdict == "ERROR"
    remaining = sorted(item.name for item in tmp_path.iterdir() if item.is_dir())
    assert remaining == sorted([str(workspace_id), ".versions"])  # clone removed


def test_cleanup_workspace_removes_clone(tmp_path):
    driver = LocalWorkspaceStorageDriver(tmp_path)
    workspace_id, path = driver.create("src")
    (Path(path) / "check.py").write_text("import sys; sys.exit(0)", encoding="utf-8")
    version_id = driver.create_version(workspace_id)
    clone_id, clone_path = driver.create_from_version(workspace_id, version_id)
    assert Path(clone_path).is_dir()

    service = EvalService(
        workspace_driver=driver,
        agentsupport=FakeAgentSupport(),  # type: ignore[arg-type]
        store=InMemoryEvalStore(),
    )
    service._cleanup_workspace(clone_id)
    assert not Path(clone_path).exists()


def test_stale_running_run_recovered_as_failed_on_service_start():
    store = InMemoryEvalStore()
    dataset = EvalDataset(name="stale", workspace_id=uuid4(), baseline_version="v1")
    store.create_dataset(dataset)
    stale = EvalRun(dataset_id=dataset.id, status="running")
    store.save_run(stale)
    done = EvalRun(dataset_id=dataset.id, status="completed")
    store.save_run(done)

    _service(store)  # constructor runs stale-run recovery

    assert stale.status == "failed"
    assert stale.summary["error"]["code"] == "EVAL_PROCESS_RESTARTED"
    assert stale.completed_at is not None
    assert done.status == "completed"


@pytest.mark.asyncio
async def test_compare_matches_by_case_id_not_task_text():
    service = _service()
    dataset = service.create_dataset(name="compare", workspace_id=uuid4(), baseline_version="v1")
    service.add_case(dataset.id, task="original-task")

    async def pass_case(case, dataset):
        return EvalCaseResult(case_id=case.id, task=case.task, verdict="PASS", score=1.0)

    async def fail_case(case, dataset):
        return EvalCaseResult(case_id=case.id, task=case.task, verdict="FAIL", score=0.0)

    service._run_case = pass_case  # type: ignore[method-assign]
    baseline = await service.run_dataset(dataset.id)

    dataset.cases[0].task = "renamed-task"  # task text changes, case id stays
    service._run_case = fail_case  # type: ignore[method-assign]
    candidate = await service.run_dataset(dataset.id)

    comparison = service.compare(baseline.id, candidate.id)
    assert comparison["regressions"] == 1
    assert comparison["deltas"][0]["task"] == "renamed-task"
    assert comparison["deltas"][0]["baseline_case_id"] == dataset.cases[0].id


def _postgres_service(tmp_path) -> EvalService:
    config = Settings(
        persistence_mode="postgres",
        database_url=f"sqlite:///{tmp_path / 'db.sqlite'}",
        auto_create_schema=True,
        workspace_root=tmp_path / "workspace",
        skills_root=tmp_path / "skills",
    )
    deps = build_agentsupport_dependencies(config)
    service = AgentSupportService(config, **deps)
    store = SqlAlchemyEvalStore(
        config.database_url,
        create_schema=config.auto_create_schema,
        engine=service.repository.engine,
    )
    return EvalService(
        workspace_driver=LocalWorkspaceStorageDriver(config.workspace_root),
        agentsupport=service,
        store=store,
    )


def _seed(tmp_path, service: EvalService) -> tuple[UUID, str]:
    workspace_id, path = service.workspace_driver.create("src")
    (Path(path) / "check.py").write_text("import sys; sys.exit(0)", encoding="utf-8")
    return workspace_id, service.workspace_driver.create_version(workspace_id)


def test_dataset_idempotency_survives_restart(tmp_path):
    first = _postgres_service(tmp_path)
    workspace_id, version_id = _seed(tmp_path, first)
    payload = {"name": "persisted-idem", "workspace_id": workspace_id, "baseline_version": version_id}

    created = first.create_dataset(idempotency_key="dataset-key-1", **payload)

    second = _postgres_service(tmp_path)
    replay = second.create_dataset(idempotency_key="dataset-key-1", **payload)
    assert replay.id == created.id

    with pytest.raises(ServiceError) as exc:
        second.create_dataset(
            idempotency_key="dataset-key-1",
            name="different",
            workspace_id=workspace_id,
            baseline_version=version_id,
        )
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_run_idempotency_survives_restart(tmp_path):
    first = _postgres_service(tmp_path)
    workspace_id, version_id = _seed(tmp_path, first)
    dataset = first.create_dataset(
        name="run-idem", workspace_id=workspace_id, baseline_version=version_id
    )
    first.add_case(dataset.id, task="task")

    run = await first.run_dataset(dataset.id, idempotency_key="run-key-1")
    assert run.status == "completed"

    second = _postgres_service(tmp_path)
    replay = await second.run_dataset(dataset.id, idempotency_key="run-key-1")
    assert replay.id == run.id
    assert replay.status == "completed"

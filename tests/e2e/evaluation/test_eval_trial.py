"""Evaluation layer landing experiment (Phase-1 trial slice).

The experiment proves the closed loop without a real model: a dataset whose
baseline passes its verification command must score PASS; changing only the
baseline content to a failing check must flip the verdict to FAIL, and the
comparison must report exactly one regression.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from helpers import build_test_eval_service
from httpx import ASGITransport, AsyncClient

from agentsupport.adapters.workspace.providers import LocalWorkspaceStorageDriver
from agentsupport.api import create_app
from agentsupport.bootstrap.settings import Settings
from agentsupport.evaluation import VerifierConfig

PYTHON = f'"{sys.executable}"'


def _seed_baseline(eval_service, name: str, check_source: str) -> tuple[str, str]:
    driver = eval_service.workspace_driver
    workspace_id, path = driver.create(name)
    (Path(path) / "check.py").write_text(check_source, encoding="utf-8")
    version_id = driver.create_version(workspace_id, name=name)
    return str(workspace_id), version_id


def _test_command_config() -> list[VerifierConfig]:
    return [
        VerifierConfig(
            type="test_command",
            params={"command": f"{PYTHON} check.py", "timeout_seconds": 30},
        )
    ]


@pytest.fixture
def env(tmp_path):
    config = Settings(
        workspace_root=tmp_path / "workspace",
        skills_root=tmp_path / "skills",
    )
    return SimpleNamespace(config=config, eval_service=build_test_eval_service(config))


def test_create_from_version_clones_snapshot(tmp_path):
    driver = LocalWorkspaceStorageDriver(tmp_path)
    workspace_id, path = driver.create("src")
    (Path(path) / "file.txt").write_text("v1", encoding="utf-8")
    version_id = driver.create_version(workspace_id)

    clone_id, clone_path = driver.create_from_version(workspace_id, version_id)
    assert clone_id != workspace_id
    assert (Path(clone_path) / "file.txt").read_text(encoding="utf-8") == "v1"
    with pytest.raises(FileNotFoundError):
        driver.create_from_version(workspace_id, "missing-version")


@pytest.mark.asyncio
async def test_good_baseline_passes_and_broken_baseline_regresses(env):
    # 1. good baseline -> PASS
    good_ws, good_version = _seed_baseline(
        env.eval_service, "good-baseline", "import sys; sys.exit(0)"
    )
    dataset = env.eval_service.create_dataset(
        name="code-regression",
        workspace_id=UUID(good_ws),
        baseline_version=good_version,
    )
    env.eval_service.add_case(
        dataset.id,
        task="make check.py pass",
        tags=["trial"],
        verifiers=_test_command_config(),
    )
    run = await env.eval_service.run_dataset(
        dataset.id, runner_fingerprint={"mode": "deterministic", "version": "0.1.0"}
    )
    assert run.status == "completed"
    assert run.summary["passed"] == 1
    assert run.summary["failed"] == 0
    assert run.summary["pass_rate"] == 1.0
    assert run.results[0].verdict == "PASS"

    # 2. broken baseline (only the workspace content changes) -> FAIL
    bad_ws, bad_version = _seed_baseline(
        env.eval_service, "broken-baseline", "import sys; sys.exit(1)"
    )
    broken_dataset = env.eval_service.create_dataset(
        name="code-regression-broken",
        workspace_id=UUID(bad_ws),
        baseline_version=bad_version,
    )
    env.eval_service.add_case(
        broken_dataset.id,
        task="make check.py pass",
        tags=["trial"],
        verifiers=_test_command_config(),
    )
    broken_run = await env.eval_service.run_dataset(
        broken_dataset.id,
        runner_fingerprint={"mode": "deterministic", "version": "0.1.0"},
    )
    assert broken_run.summary["failed"] == 1
    assert broken_run.results[0].verdict == "FAIL"
    assert "exit 1" in broken_run.results[0].reasons[0]

    # 3. comparison detects the regression
    comparison = env.eval_service.compare(run.id, broken_run.id)
    assert comparison["same_fingerprint"] is True
    assert comparison["regressions"] == 1
    assert comparison["deltas"][0]["baseline"] == "PASS"
    assert comparison["deltas"][0]["candidate"] == "FAIL"
    assert comparison["deltas"][0]["change"] == "regression"


@pytest.mark.asyncio
async def test_eval_http_contract(env):
    app = create_app(eval_service=env.eval_service)
    workspace_id, version_id = _seed_baseline(
        env.eval_service, "http-baseline", "import sys; sys.exit(0)"
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/eval/datasets",
            json={
                "name": "http-dataset",
                "workspace_id": workspace_id,
                "baseline_version": version_id,
            },
            headers={"Idempotency-Key": "dataset-key-1"},
        )
        assert created.status_code == 201
        dataset_id = created.json()["id"]

        replay = await client.post(
            "/eval/datasets",
            json={
                "name": "http-dataset",
                "workspace_id": workspace_id,
                "baseline_version": version_id,
            },
            headers={"Idempotency-Key": "dataset-key-1"},
        )
        assert replay.json()["id"] == dataset_id

        case = await client.post(
            f"/eval/datasets/{dataset_id}/cases",
            json={
                "task": "make check.py pass",
                "verifiers": [
                    {
                        "type": "test_command",
                        "params": {"command": f"{PYTHON} check.py", "timeout_seconds": 30},
                    }
                ],
            },
        )
        assert case.status_code == 201

        run = await client.post(
            "/eval/runs",
            json={"dataset_id": dataset_id, "runner_fingerprint": {"mode": "deterministic"}},
        )
        assert run.status_code == 202
        run_id = run.json()["id"]
        assert run.json()["status"] == "running"

        for _ in range(200):
            polled = await client.get(f"/eval/runs/{run_id}")
            assert polled.status_code == 200
            if polled.json()["status"] in {"completed", "failed"}:
                break
            await asyncio.sleep(0.01)
        assert polled.json()["status"] == "completed"
        assert polled.json()["summary"]["passed"] == 1

        report = await client.get(f"/eval/runs/{run_id}/report")
        assert report.status_code == 200
        assert report.json()["summary"]["passed"] == 1
        assert report.json()["cases"][0]["verdict"] == "PASS"

        missing = await client.get(f"/eval/runs/{uuid4()}/report")
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_unknown_verifier_is_error_not_fail(env):
    workspace_id, version_id = _seed_baseline(
        env.eval_service, "error-baseline", "import sys; sys.exit(0)"
    )
    dataset = env.eval_service.create_dataset(
        name="error-dataset",
        workspace_id=UUID(workspace_id),
        baseline_version=version_id,
    )
    env.eval_service.add_case(
        dataset.id,
        task="task",
        verifiers=[VerifierConfig(type="no_such_verifier")],
    )
    run = await env.eval_service.run_dataset(dataset.id)
    assert run.summary["error"] == 1
    assert run.summary["pass_rate"] == 0.0
    assert run.results[0].verifier_results[0].status == "ERROR"


@pytest.mark.asyncio
async def test_persistent_mode_run_survives_restart(tmp_path):
    """Postgres-mode wiring persists runs; a fresh service can reload them."""

    config = Settings(
        persistence_mode="postgres",
        database_url=f"sqlite:///{tmp_path / 'eval.db'}",
        auto_create_schema=True,
        workspace_root=tmp_path / "workspace",
        skills_root=tmp_path / "skills",
    )
    first = build_test_eval_service(config)
    workspace_id, version_id = _seed_baseline(
        first, "persist-baseline", "import sys; sys.exit(0)"
    )
    dataset = first.create_dataset(
        name="persist-dataset",
        workspace_id=UUID(workspace_id),
        baseline_version=version_id,
    )
    first.add_case(
        dataset.id,
        task="make check.py pass",
        verifiers=_test_command_config(),
    )
    run = await first.run_dataset(
        dataset.id, runner_fingerprint={"mode": "deterministic", "version": "0.1.0"}
    )
    assert run.summary["passed"] == 1

    # simulate restart: a brand-new service over the same database
    second = build_test_eval_service(config)
    reloaded = second.get_run(run.id)
    assert reloaded is not None
    assert reloaded.summary["passed"] == 1
    assert reloaded.results[0].verdict == "PASS"
    datasets = second.list_datasets()
    assert [item.name for item in datasets] == ["persist-dataset"]

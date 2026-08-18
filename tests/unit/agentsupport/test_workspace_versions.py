"""Workspace version snapshots: driver, service and HTTP contract."""

from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from agentsupport.adapters.workspace.providers import LocalWorkspaceStorageDriver
from agentsupport.api import create_app
from agentsupport.application.service import ServiceError
from agentsupport.bootstrap.settings import Settings
from agentsupport.services import AgentSupportService


def _make_service(tmp_path: Path, *, kubernetes: bool = False) -> AgentSupportService:
    if kubernetes:
        return AgentSupportService(
            Settings(workspace_root=tmp_path, runtime_driver="kubernetes")
        )
    return AgentSupportService(Settings(workspace_root=tmp_path))


def test_local_storage_driver_round_trip(tmp_path: Path):
    driver = LocalWorkspaceStorageDriver(tmp_path)
    workspace_id, path = driver.create("demo")
    root = Path(path)
    (root / "file.txt").write_text("v1", encoding="utf-8")

    version_id = driver.create_version(workspace_id, name="baseline")
    assert version_id
    versions = driver.list_versions(workspace_id)
    assert [item["version_id"] for item in versions] == [version_id]
    assert versions[0]["name"] == "baseline"

    # mutate the workspace, then restore the baseline
    (root / "file.txt").write_text("v2", encoding="utf-8")
    (root / "extra.txt").write_text("extra", encoding="utf-8")
    (root / ".agentsupport" / "trajectories").mkdir(parents=True)
    (root / ".agentsupport" / "trajectories" / "run.json").write_text("{}", encoding="utf-8")

    driver.restore_version(workspace_id, version_id)
    assert (root / "file.txt").read_text(encoding="utf-8") == "v1"
    assert not (root / "extra.txt").exists()
    assert not (root / ".agentsupport").exists()

    with pytest.raises(FileNotFoundError):
        driver.restore_version(workspace_id, uuid4().hex)


def test_service_version_lifecycle_and_idempotency(tmp_path: Path):
    service = _make_service(tmp_path)
    workspace = service.create_workspace("demo")
    root = tmp_path / str(workspace.id)
    (root / "file.txt").write_text("v1", encoding="utf-8")

    created = service.create_workspace_version(
        workspace.id, name="baseline", idempotency_key="key-1"
    )
    assert created["workspace_id"] == str(workspace.id)
    assert created["name"] == "baseline"

    replay = service.create_workspace_version(
        workspace.id, name="baseline", idempotency_key="key-1"
    )
    assert replay["version_id"] == created["version_id"]

    with pytest.raises(ServiceError) as excinfo:
        service.create_workspace_version(
            workspace.id, name="different", idempotency_key="key-1"
        )
    assert excinfo.value.code == "IDEMPOTENCY_CONFLICT"
    assert excinfo.value.status_code == 409

    listed = service.list_workspace_versions(workspace.id)
    assert [item["version_id"] for item in listed] == [created["version_id"]]

    (root / "file.txt").write_text("v2", encoding="utf-8")
    restored = service.restore_workspace_version(workspace.id, created["version_id"])
    assert restored["idempotent_replay"] is False
    assert (root / "file.txt").read_text(encoding="utf-8") == "v1"

    replay_restore = service.restore_workspace_version(
        workspace.id, created["version_id"], idempotency_key="restore-1"
    )
    assert replay_restore["idempotent_replay"] is False
    replay_restore = service.restore_workspace_version(
        workspace.id, created["version_id"], idempotency_key="restore-1"
    )
    assert replay_restore["idempotent_replay"] is True


def test_service_restore_guards_active_workspace(tmp_path: Path):
    service = _make_service(tmp_path)
    workspace = service.create_workspace("demo")
    version = service.create_workspace_version(workspace.id)
    session = service.create_session(workspace.id)
    service.workspace_leases[workspace.id] = session.id

    with pytest.raises(ServiceError) as excinfo:
        service.restore_workspace_version(workspace.id, version["version_id"])
    assert excinfo.value.code == "WORKSPACE_BUSY"
    assert excinfo.value.status_code == 409


def test_service_rejects_unsupported_storage(tmp_path: Path):
    service = _make_service(tmp_path, kubernetes=True)
    workspace = service.create_workspace("demo")

    with pytest.raises(ServiceError) as excinfo:
        service.create_workspace_version(workspace.id)
    assert excinfo.value.code == "WORKSPACE_VERSIONING_UNSUPPORTED"
    assert excinfo.value.status_code == 501

    with pytest.raises(ServiceError):
        service.list_workspace_versions(workspace.id)


def test_service_restore_unknown_version_404(tmp_path: Path):
    service = _make_service(tmp_path)
    workspace = service.create_workspace("demo")

    with pytest.raises(ServiceError) as excinfo:
        service.restore_workspace_version(workspace.id, uuid4().hex)
    assert excinfo.value.code == "WORKSPACE_VERSION_NOT_FOUND"
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_http_contract_workspace_versions(tmp_path: Path):
    service = _make_service(tmp_path)
    app = create_app(service)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        workspace = (
            await client.post("/workspaces", json={"name": "demo"})
        ).json()
        workspace_id = workspace["id"]
        root = tmp_path / workspace_id
        (root / "file.txt").write_text("v1", encoding="utf-8")

        created = await client.post(
            f"/workspaces/{workspace_id}/versions",
            json={"name": "baseline"},
            headers={"Idempotency-Key": "http-key-1"},
        )
        assert created.status_code == 201
        version_id = created.json()["version_id"]

        replay = await client.post(
            f"/workspaces/{workspace_id}/versions",
            json={"name": "baseline"},
            headers={"Idempotency-Key": "http-key-1"},
        )
        assert replay.json()["version_id"] == version_id

        listed = await client.get(f"/workspaces/{workspace_id}/versions")
        assert listed.status_code == 200
        assert [item["version_id"] for item in listed.json()] == [version_id]

        (root / "file.txt").write_text("v2", encoding="utf-8")
        restored = await client.post(
            f"/workspaces/{workspace_id}/versions/{version_id}/restore"
        )
        assert restored.status_code == 200
        assert restored.json()["idempotent_replay"] is False
        assert (root / "file.txt").read_text(encoding="utf-8") == "v1"

        missing = await client.post(
            f"/workspaces/{workspace_id}/versions/{uuid4().hex}/restore"
        )
        assert missing.status_code == 404
        assert missing.json()["code"] == "WORKSPACE_VERSION_NOT_FOUND"

        unknown = await client.post(f"/workspaces/{uuid4()}/versions")
        assert unknown.status_code == 404
        assert unknown.json()["code"] == "WORKSPACE_NOT_FOUND"

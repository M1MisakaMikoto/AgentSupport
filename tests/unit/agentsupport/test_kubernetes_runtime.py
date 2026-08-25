import json
from uuid import uuid4

import httpx
import pytest

from agentsupport.kubernetes_runtime import KubernetesRuntimeDriver


@pytest.mark.asyncio
async def test_kubernetes_runtime_creates_fenced_runner_and_preserves_workspace_pvc():
    resources = {
        "persistentvolumeclaims": {},
        "services": {},
        "pods": {},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-token"
        parts = request.url.path.strip("/").split("/")
        resource = parts[4]
        name = parts[5] if len(parts) > 5 else None
        if request.method == "GET" and name:
            manifest = resources[resource].get(name)
            if manifest is None:
                return httpx.Response(404)
            body = json.loads(json.dumps(manifest))
            if resource == "pods":
                body["status"] = {
                    "phase": "Running",
                    "containerStatuses": [{"name": "runner", "ready": True}],
                }
            return httpx.Response(200, json=body)
        if request.method == "POST":
            manifest = json.loads(request.content)
            resources[resource][manifest["metadata"]["name"]] = manifest
            return httpx.Response(201, json=manifest)
        if request.method == "DELETE":
            resources[resource].pop(name, None)
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected Kubernetes request: {request.method} {request.url}")

    driver = KubernetesRuntimeDriver(
        api_server="https://kubernetes.test",
        namespace="agents",
        image="agentsupport-runner:test",
        token="test-token",
        startup_timeout_seconds=1,
        poll_interval_seconds=0,
        container_env={"SESSION_RUNNER_MODE": "deterministic"},
        transport=httpx.MockTransport(handler),
    )
    session_id = uuid4()
    workspace_id = uuid4()

    runtime_id = await driver.start(
        session_id, "/unused", 7, workspace_id, read_only_mounts=[]
    )

    pod = resources["pods"][runtime_id]
    assert pod["metadata"]["labels"]["agentsupport/lease-epoch"] == "7"
    container = pod["spec"]["containers"][0]
    assert container["resources"] == {
        "requests": {"cpu": "500m", "memory": "1Gi"},
        "limits": {"cpu": "2", "memory": "2Gi"},
    }
    assert pod["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] in resources[
        "persistentvolumeclaims"
    ]
    assert {item["name"]: item["value"] for item in container["env"]}["SESSION_LEASE_EPOCH"] == "7"
    assert await driver.endpoint(runtime_id) == f"http://{runtime_id}.agents.svc:8080"
    assert (await driver.inspect(runtime_id))["status"] == "running"

    assert await driver.stop(runtime_id) is True
    assert resources["pods"] == {}
    assert resources["services"] == {}
    assert len(resources["persistentvolumeclaims"]) == 1


@pytest.mark.asyncio
async def test_kubernetes_runtime_rejects_host_path_mounts():
    driver = KubernetesRuntimeDriver(token="test", transport=httpx.MockTransport(lambda _: None))
    with pytest.raises(ValueError, match="host-path"):
        await driver.start(uuid4(), "/workspace", 1, uuid4(), [("local", "/skill")])

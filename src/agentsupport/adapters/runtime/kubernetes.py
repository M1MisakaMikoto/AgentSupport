from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx


def _resource_name(prefix: str, value: UUID | str, suffix: str | int | None = None) -> str:
    raw = f"{prefix}-{value}" + (f"-{suffix}" if suffix is not None else "")
    normalized = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")
    return normalized[:63].rstrip("-")


class KubernetesRuntimeDriver:
    """Creates one fenced Runner Pod per active Session using the Kubernetes API."""

    def __init__(
        self,
        *,
        api_server: str = "https://kubernetes.default.svc",
        namespace: str = "default",
        image: str = "agentsupport-runner:dev",
        token: str | None = None,
        token_path: Path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token"),
        ca_path: Path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"),
        pvc_size: str = "10Gi",
        storage_class: str | None = None,
        runner_secret_name: str | None = None,
        startup_timeout_seconds: float = 60,
        poll_interval_seconds: float = 0.5,
        pod_cpu_request: str = "500m",
        pod_memory_request: str = "1Gi",
        pod_cpu_limit: str = "2",
        pod_memory_limit: str = "2Gi",
        container_env: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_server = api_server.rstrip("/")
        self.namespace = namespace
        self.image = image
        self.token = token
        self.token_path = token_path
        self.ca_path = ca_path
        self.pvc_size = pvc_size
        self.storage_class = storage_class
        self.runner_secret_name = runner_secret_name
        self.startup_timeout_seconds = startup_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.pod_cpu_request = pod_cpu_request
        self.pod_memory_request = pod_memory_request
        self.pod_cpu_limit = pod_cpu_limit
        self.pod_memory_limit = pod_memory_limit
        self.container_env = container_env or {}
        self.transport = transport

    def _token(self) -> str:
        if self.token is not None:
            return self.token
        return self.token_path.read_text(encoding="utf-8").strip()

    def _client(self) -> httpx.AsyncClient:
        verify: bool | str = str(self.ca_path) if self.ca_path.exists() else True
        return httpx.AsyncClient(
            base_url=self.api_server,
            headers={"Authorization": f"Bearer {self._token()}"},
            verify=verify,
            timeout=10,
            transport=self.transport,
        )

    @property
    def _base_path(self) -> str:
        return f"/api/v1/namespaces/{self.namespace}"

    async def _get(self, resource: str, name: str) -> dict[str, Any] | None:
        async with self._client() as client:
            response = await client.get(f"{self._base_path}/{resource}/{name}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def _create(self, resource: str, manifest: dict[str, Any]) -> None:
        async with self._client() as client:
            response = await client.post(f"{self._base_path}/{resource}", json=manifest)
        if response.status_code != 409:
            response.raise_for_status()

    async def _delete(self, resource: str, name: str) -> None:
        async with self._client() as client:
            response = await client.delete(
                f"{self._base_path}/{resource}/{name}",
                params={"propagationPolicy": "Background"},
            )
        if response.status_code != 404:
            response.raise_for_status()

    def _pvc_manifest(self, workspace_id: UUID) -> dict[str, Any]:
        spec: dict[str, Any] = {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": self.pvc_size}},
        }
        if self.storage_class:
            spec["storageClassName"] = self.storage_class
        return {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": _resource_name("workspace", workspace_id),
                "labels": {
                    "app.kubernetes.io/managed-by": "agentsupport",
                    "agentsupport/workspace-id": str(workspace_id),
                },
            },
            "spec": spec,
        }

    def _service_manifest(self, name: str, session_id: UUID) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": name,
                "labels": {"app.kubernetes.io/managed-by": "agentsupport"},
            },
            "spec": {
                "clusterIP": "None",
                "selector": {
                    "agentsupport/runtime-id": name,
                    "agentsupport/session-id": str(session_id),
                },
                "ports": [{"name": "http", "port": 8080, "targetPort": 8080}],
            },
        }

    def _pod_manifest(
        self,
        name: str,
        session_id: UUID,
        workspace_id: UUID,
        lease_epoch: int,
    ) -> dict[str, Any]:
        env = {**self.container_env, "SESSION_LEASE_EPOCH": str(lease_epoch)}
        container: dict[str, Any] = {
            "name": "runner",
            "image": self.image,
            "imagePullPolicy": "IfNotPresent",
            "command": [
                "uvicorn",
                "session_runner.main:app",
                "--host",
                "0.0.0.0",
                "--port",
                "8080",
            ],
            "env": [{"name": key, "value": value} for key, value in sorted(env.items())],
            "ports": [{"name": "http", "containerPort": 8080}],
            "resources": {
                "requests": {
                    "cpu": self.pod_cpu_request,
                    "memory": self.pod_memory_request,
                },
                "limits": {
                    "cpu": self.pod_cpu_limit,
                    "memory": self.pod_memory_limit,
                },
            },
            "readinessProbe": {
                "httpGet": {"path": "/ready", "port": "http"},
                "periodSeconds": 2,
                "failureThreshold": 30,
            },
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": False,
                "runAsNonRoot": True,
                "capabilities": {"drop": ["ALL"]},
            },
            "volumeMounts": [{"name": "workspace", "mountPath": "/workspace"}],
        }
        if self.runner_secret_name:
            container["envFrom"] = [{"secretRef": {"name": self.runner_secret_name}}]
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": name,
                "labels": {
                    "app.kubernetes.io/name": "agentsupport-runner",
                    "app.kubernetes.io/managed-by": "agentsupport",
                    "agentsupport/runtime-id": name,
                    "agentsupport/session-id": str(session_id),
                    "agentsupport/workspace-id": str(workspace_id),
                    "agentsupport/lease-epoch": str(lease_epoch),
                },
            },
            "spec": {
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "securityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [container],
                "volumes": [
                    {
                        "name": "workspace",
                        "persistentVolumeClaim": {
                            "claimName": _resource_name("workspace", workspace_id)
                        },
                    }
                ],
            },
        }

    async def start(
        self,
        session_id: UUID,
        workspace_path: str,
        lease_epoch: int,
        workspace_id: UUID | None = None,
        read_only_mounts: list[tuple[str, str]] | None = None,
        runtime_operation_id: UUID | None = None,
    ) -> str:
        del workspace_path, runtime_operation_id
        if workspace_id is None:
            raise ValueError("Kubernetes runtime requires workspace_id")
        if read_only_mounts:
            raise ValueError("Kubernetes runtime does not accept host-path skill mounts")
        name = _resource_name("runner", session_id, lease_epoch)
        pvc_name = _resource_name("workspace", workspace_id)
        if await self._get("persistentvolumeclaims", pvc_name) is None:
            await self._create("persistentvolumeclaims", self._pvc_manifest(workspace_id))
        if await self._get("services", name) is None:
            await self._create("services", self._service_manifest(name, session_id))
        if await self._get("pods", name) is None:
            await self._create(
                "pods", self._pod_manifest(name, session_id, workspace_id, lease_epoch)
            )
        deadline = asyncio.get_running_loop().time() + self.startup_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            pod = await self._get("pods", name)
            statuses = (pod or {}).get("status", {}).get("containerStatuses", [])
            if (pod or {}).get("status", {}).get("phase") == "Running" and any(
                status.get("ready") for status in statuses
            ):
                return name
            await asyncio.sleep(self.poll_interval_seconds)
        await self.stop(name, force=True)
        raise TimeoutError(f"Runner Pod did not become ready: {name}")

    async def stop(self, container_id: str, *, force: bool = False) -> bool:
        del force
        await self._delete("pods", container_id)
        await self._delete("services", container_id)
        return True

    async def inspect(self, container_id: str) -> dict[str, Any]:
        pod = await self._get("pods", container_id)
        if pod is None:
            return {"status": "missing"}
        phase = pod.get("status", {}).get("phase", "Unknown")
        status = {
            "Pending": "created",
            "Running": "running",
            "Succeeded": "exited",
            "Failed": "exited",
        }.get(phase, phase.lower())
        return {"status": status, "phase": phase, "pod": pod}

    async def endpoint(self, container_id: str) -> str | None:
        service = await self._get("services", container_id)
        if service is None:
            return None
        return f"http://{container_id}.{self.namespace}.svc:8080"

    async def list_managed(self) -> list[dict[str, Any]]:
        async with self._client() as client:
            response = await client.get(
                f"{self._base_path}/pods",
                params={"labelSelector": "app.kubernetes.io/managed-by=agentsupport"},
            )
        response.raise_for_status()
        return list(response.json().get("items", []))

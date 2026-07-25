from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx


class DockerRuntimeDriver:
    """Docker boundary; command execution is deliberately isolated behind this port."""

    def __init__(self, image: str = "agent-session:dev") -> None:
        self.image = image
        self._containers: dict[str, dict[str, Any]] = {}

    async def start(
        self,
        session_id: UUID,
        workspace_path: str,
        lease_epoch: int,
        workspace_id: UUID | None = None,
        read_only_mounts: list[tuple[str, str]] | None = None,
        runtime_operation_id: UUID | None = None,
    ) -> str:
        container_id = f"session-{uuid.uuid4().hex[:12]}"
        self._containers[container_id] = {
            "status": "running",
            "session_id": str(session_id),
            "workspace_path": workspace_path,
            "workspace_id": str(workspace_id) if workspace_id else None,
            "lease_epoch": lease_epoch,
            "image": self.image,
            "read_only_mounts": read_only_mounts or [],
            "runtime_operation_id": str(runtime_operation_id) if runtime_operation_id else None,
        }
        return container_id

    async def stop(self, container_id: str, *, force: bool = False) -> bool:
        container = self._containers.get(container_id)
        if container is None:
            return True
        container["status"] = "exited" if not force else "killed"
        await asyncio.sleep(0)
        return True

    async def inspect(self, container_id: str) -> dict[str, Any]:
        return dict(self._containers.get(container_id, {"status": "missing"}))

    async def endpoint(self, container_id: str) -> str | None:
        return None


class DockerCliRuntimeDriver:
    """Development Docker driver using the WSL2-backed Docker CLI context."""

    def __init__(
        self,
        image: str = "agent-session:dev",
        context: str = "desktop-linux",
        stop_grace_seconds: int = 30,
        startup_timeout_seconds: int = 30,
        endpoint_host: str = "127.0.0.1",
        runner_port: int = 8080,
        container_env: dict[str, str] | None = None,
    ) -> None:
        self.image = image
        self.context = context
        self.stop_grace_seconds = stop_grace_seconds
        self.startup_timeout_seconds = startup_timeout_seconds
        self.endpoint_host = endpoint_host
        self.runner_port = runner_port
        self.container_env = dict(container_env or {})
        self._endpoints: dict[str, str] = {}

    async def _command(self, *args: str, check: bool = True) -> str:
        process = await asyncio.create_subprocess_exec(
            "docker",
            "--context",
            self.context,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if check and process.returncode:
            message = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace")
            raise RuntimeError(f"docker command failed ({process.returncode}): {message}")
        return stdout.decode(errors="replace").strip()

    @staticmethod
    def _container_name(session_id: UUID, lease_epoch: int) -> str:
        return f"agent-session-{session_id.hex[:12]}-{lease_epoch}"

    async def start(
        self,
        session_id: UUID,
        workspace_path: str,
        lease_epoch: int,
        workspace_id: UUID | None = None,
        read_only_mounts: list[tuple[str, str]] | None = None,
        runtime_operation_id: UUID | None = None,
    ) -> str:
        name = self._container_name(session_id, lease_epoch)
        existing = await self.inspect(name)
        if existing.get("status") == "running":
            container_id = existing["id"]
            endpoint = self._endpoint_from_inspection(existing)
            if endpoint:
                self._endpoints[container_id] = endpoint
                await self._wait_ready(endpoint)
            return container_id
        if existing.get("status") not in {None, "missing"}:
            await self._command("rm", "-f", name, check=False)
        source = str(Path(workspace_path).resolve())
        if not Path(source).is_dir():
            raise ValueError(f"workspace path does not exist: {source}")
        labels = [
            "--label",
            f"session_id={session_id}",
            "--label",
            f"workspace_id={workspace_id or ''}",
            "--label",
            f"lease_epoch={lease_epoch}",
        ]
        if runtime_operation_id:
            labels.extend(["--label", f"runtime_operation_id={runtime_operation_id}"])
        skill_mounts: list[str] = []
        for source_path, target_path in read_only_mounts or []:
            self._validate_read_only_mount(source_path, target_path)
            skill_mounts.extend(
                [
                    "--mount",
                    f"type=bind,source={Path(source_path).resolve()},target={target_path},readonly",
                ]
            )
        runtime_environment = dict(self.container_env)
        runtime_environment.update(
            {
                "SESSION_ID": str(session_id),
                "SESSION_WORKSPACE_ID": str(workspace_id or ""),
                "SESSION_LEASE_EPOCH": str(lease_epoch),
            }
        )
        environment = [
            value
            for key, item in sorted(runtime_environment.items())
            for value in ("--env", f"{key}={item}")
        ]
        output = await self._command(
            "run",
            "-d",
            "--name",
            name,
            "--user",
            "agent",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit",
            "256",
            "--memory",
            "2g",
            "--cpus",
            "2",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--publish",
            f"{self.endpoint_host}::{self.runner_port}",
            *labels,
            *skill_mounts,
            *environment,
            "--mount",
            f"type=bind,source={source},target=/workspace",
            self.image,
        )
        container_id = output.splitlines()[-1]
        inspection = await self.inspect(container_id)
        endpoint = self._endpoint_from_inspection(inspection)
        if not endpoint:
            await self._command("rm", "-f", container_id, check=False)
            raise RuntimeError("Docker container did not publish the Session Runner port")
        self._endpoints[container_id] = endpoint
        try:
            await self._wait_ready(endpoint)
        except Exception:
            await self._command("rm", "-f", container_id, check=False)
            self._endpoints.pop(container_id, None)
            raise
        return container_id

    async def stop(self, container_id: str, *, force: bool = False) -> bool:
        inspect = await self.inspect(container_id)
        status = inspect.get("status")
        if status in {None, "missing"}:
            self._endpoints.pop(container_id, None)
            return True
        if status in {"exited", "dead"}:
            await self._command("rm", "-f", container_id, check=False)
            self._endpoints.pop(container_id, None)
            return True
        if force:
            await self._command("kill", container_id, check=False)
            after = await self.inspect(container_id)
            stopped = after.get("status") in {"exited", "dead", "missing"}
            if stopped:
                await self._command("rm", "-f", container_id, check=False)
                self._endpoints.pop(container_id, None)
            return stopped
        await self._command(
            "stop", "--time", str(self.stop_grace_seconds), container_id, check=False
        )
        after = await self.inspect(container_id)
        if after.get("status") not in {"exited", "dead", "missing"}:
            await self._command("kill", container_id, check=False)
            after = await self.inspect(container_id)
        stopped = after.get("status") in {"exited", "dead", "missing"}
        if stopped:
            await self._command("rm", "-f", container_id, check=False)
            self._endpoints.pop(container_id, None)
        return stopped

    async def inspect(self, container_id: str) -> dict[str, Any]:
        output = await self._command("inspect", container_id, check=False)
        if not output:
            return {"status": "missing"}
        try:
            items = json.loads(output)
            if not items:
                return {"status": "missing"}
            item = items[0]
        except (ValueError, TypeError, IndexError) as exc:
            raise RuntimeError(f"invalid docker inspect output for {container_id}") from exc
        config = item.get("Config", {})
        ports = item.get("NetworkSettings", {}).get("Ports", {}) or {}
        published = ports.get(f"{self.runner_port}/tcp") or []
        host_port = published[0].get("HostPort") if published else None
        return {
            "id": item.get("Id", container_id),
            "status": item.get("State", {}).get("Status", "unknown"),
            "labels": config.get("Labels", {}),
            "runner_port": int(host_port) if host_port else None,
            "name": item.get("Name", "").lstrip("/"),
            "mounts": [
                {"source": mount.get("Source"), "target": mount.get("Destination")}
                for mount in item.get("Mounts", [])
            ],
        }

    async def endpoint(self, container_id: str) -> str | None:
        endpoint = self._endpoints.get(container_id)
        if endpoint:
            return endpoint
        inspection = await self.inspect(container_id)
        endpoint = self._endpoint_from_inspection(inspection)
        if endpoint:
            self._endpoints[container_id] = endpoint
        return endpoint

    def _endpoint_from_inspection(self, inspection: dict[str, Any]) -> str | None:
        port = inspection.get("runner_port")
        if not port:
            return None
        return f"http://{self.endpoint_host}:{port}"

    @staticmethod
    def _validate_read_only_mount(source_path: str, target_path: str) -> None:
        if not Path(source_path).is_file() and not Path(source_path).is_dir():
            raise ValueError(f"read-only mount source does not exist: {source_path}")
        if not target_path.startswith("/opt/agent-skills/"):
            raise ValueError(
                f"read-only mount target is outside the Skill namespace: {target_path}"
            )

    async def _wait_ready(self, endpoint: str) -> None:
        deadline = asyncio.get_running_loop().time() + self.startup_timeout_seconds
        last_error: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                async with httpx.AsyncClient(base_url=endpoint, timeout=1.0) as client:
                    response = await client.get("/ready")
                    response.raise_for_status()
                    return
            except Exception as exc:  # noqa: BLE001 - startup polling
                last_error = exc
                await asyncio.sleep(0.25)
        raise RuntimeError(f"Session Runner did not become ready at {endpoint}: {last_error}")


def default_session_container_env() -> dict[str, str]:
    """Return only the model settings explicitly allowed into Session containers."""

    keys = (
        "SESSION_RUNNER_MODE",
        "SESSION_RUNNER_TRAE_CONFIG",
        "SESSION_RUNNER_WORKSPACE_ROOTS",
        "TRAE_PROVIDER",
        "TRAE_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "TRAE_MODEL_BASE_URL",
        "ANTHROPIC_BASE_URL",
        "TRAE_MODEL",
        "TRAE_MAX_STEPS",
    )
    environment = {key: value for key in keys if (value := os.getenv(key))}
    # Match the Compose runner defaults while keeping credentials process-only.
    if "TRAE_API_KEY" not in environment and "ANTHROPIC_AUTH_TOKEN" in environment:
        environment["TRAE_API_KEY"] = environment["ANTHROPIC_AUTH_TOKEN"]
    if "TRAE_MODEL_BASE_URL" not in environment and "ANTHROPIC_BASE_URL" in environment:
        environment["TRAE_MODEL_BASE_URL"] = environment["ANTHROPIC_BASE_URL"]
    environment.setdefault("SESSION_RUNNER_MODE", "trae")
    environment.setdefault("SESSION_RUNNER_TRAE_CONFIG", "/app/session_runner/trae_config.yaml")
    environment.setdefault("SESSION_RUNNER_WORKSPACE_ROOTS", "/workspace")
    return environment

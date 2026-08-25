from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .api_acceptance import ApiContractVerifier
from .monitor import EnvironmentMonitor

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTROL_UI_ROOT = Path(__file__).parent / "static" / "control"
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
WSL_COMPOSE_ENV_KEYS = {
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "COMPOSE_PROFILES",
    "COMPOSE_PROJECT_NAME",
    "SESSION_RUNNER_MODE",
    "TRAE_API_KEY",
    "TRAE_MAX_STEPS",
    "TRAE_MODEL",
    "TRAE_MODEL_BASE_URL",
    "TRAE_PROVIDER",
}
WSL_BUILD_SOURCES = (
    "Dockerfile",
    ".dockerignore",
    "pyproject.toml",
    "requirements.txt",
    "alembic.ini",
    "alembic",
    "src",
    "vendor/trae-agent-src",
)
WSL_BUILD_EXCLUDES = (
    ".git",
    ".venv",
    ".pytest_cache",
    ".ruff_cache",
    ".tmp",
    "__pycache__",
    "*.pyc",
)
LAN_FORWARD_SCRIPT = "wsl-lan-forward.ps1"
LAN_FORWARD_DISABLED = {"off", "0", "false", "no"}


def _now() -> datetime:
    return datetime.now(UTC)


class DockerTargetRequest(BaseModel):
    docker_transport: Literal["auto", "local", "context", "wsl2"] = "auto"
    docker_context: str = Field(default="", max_length=128)
    wsl_distribution: str = Field(default="", max_length=128)


class StackRequest(DockerTargetRequest):
    api_replicas: int = Field(default=2, ge=1, le=8)
    worker_replicas: int = Field(default=3, ge=1, le=16)
    runner_mode: Literal["deterministic", "trae"] = "deterministic"
    rebuild: bool = True


class AcceptanceRequest(DockerTargetRequest):
    expected_api_replicas: int = Field(default=2, ge=1, le=8)
    include_postgres: bool = True
    include_task_smoke: bool = True


class ApiAcceptanceRequest(BaseModel):
    include_task_flow: bool = True
    include_negative: bool = True
    include_sse: bool = True
    timeout_seconds: int = Field(default=30, ge=5, le=120)


@dataclass(slots=True)
class CommandResult:
    returncode: int
    stdout: list[str] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ConsoleOperation:
    id: str
    action: str
    status: Literal["queued", "running", "succeeded", "failed"] = "queued"
    created_at: datetime = field(default_factory=_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    logs: deque[dict[str, str]] = field(default_factory=lambda: deque(maxlen=2000))
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def add_log(self, stream: str, message: str) -> None:
        self.logs.append(
            {
                "at": _now().isoformat(),
                "stream": stream,
                "message": message,
            }
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "steps": self.steps,
            "logs": list(self.logs),
            "result": self.result,
            "error": self.error,
        }


class SubprocessCommandRunner:
    async def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None,
        emit: Callable[[str, str], None],
    ) -> CommandResult:
        emit("command", " ".join(command))
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            env={**os.environ, **(env or {})},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        stdout: list[str] = []
        stderr: list[str] = []

        async def consume(reader: asyncio.StreamReader, stream: str, target: list[str]) -> None:
            while line := await reader.readline():
                text = line.decode(errors="replace").rstrip()
                target.append(text)
                emit(stream, text)

        assert process.stdout is not None
        assert process.stderr is not None
        await asyncio.gather(
            consume(process.stdout, "stdout", stdout),
            consume(process.stderr, "stderr", stderr),
        )
        returncode = await process.wait()
        if returncode:
            detail = stderr[-1] if stderr else stdout[-1] if stdout else "command failed"
            raise RuntimeError(f"command exited with {returncode}: {detail}")
        return CommandResult(returncode=returncode, stdout=stdout, stderr=stderr)


class DevConsoleController:
    def __init__(
        self,
        *,
        project_root: Path = PROJECT_ROOT,
        command_runner: SubprocessCommandRunner | None = None,
        docker_transport: str | None = None,
        docker_context: str | None = None,
        wsl_distribution: str | None = None,
        agentsupport_url: str | None = None,
        agentsupport_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.command_runner = command_runner or SubprocessCommandRunner()
        self.docker_context = docker_context or os.getenv("AGENTSUPPORT_DEV_DOCKER_CONTEXT", "")
        configured_transport = docker_transport or os.getenv("AGENTSUPPORT_DEV_DOCKER_TRANSPORT", "")
        if not configured_transport:
            configured_transport = "context" if self.docker_context else (
                "wsl2" if os.name == "nt" else "local"
            )
        if configured_transport not in {"local", "context", "wsl2"}:
            raise ValueError(f"unsupported Docker transport: {configured_transport}")
        self.docker_transport = configured_transport
        self.wsl_distribution = wsl_distribution or os.getenv(
            "AGENTSUPPORT_DEV_WSL_DISTRIBUTION", ""
        )
        self.agentsupport_url = (agentsupport_url or os.getenv(
            "AGENTSUPPORT_DEV_API_URL", "http://127.0.0.1:8000"
        )).rstrip("/")
        self.agentsupport_transport = agentsupport_transport
        self.operations: dict[str, ConsoleOperation] = {}
        self._active_id: str | None = None
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self.on_operation_finished: Callable[[ConsoleOperation], None] | None = None

    def _target(self, request: DockerTargetRequest | StackRequest | AcceptanceRequest | None) -> dict[str, str]:
        requested_transport = request.docker_transport if request else "auto"
        transport = self.docker_transport if requested_transport == "auto" else requested_transport
        context = (request.docker_context if request else "") or self.docker_context
        distribution = (request.wsl_distribution if request else "") or self.wsl_distribution
        if transport == "context" and not context:
            raise ValueError("Docker context 模式需要填写 context 名称")
        return {
            "transport": transport,
            "docker_context": context,
            "wsl_distribution": distribution,
        }

    def _docker(
        self,
        *arguments: str,
        target: DockerTargetRequest | StackRequest | AcceptanceRequest | None = None,
    ) -> list[str]:
        selected = self._target(target)
        if selected["transport"] == "local":
            return ["docker", *arguments]
        if selected["transport"] == "context":
            return ["docker", "--context", selected["docker_context"], *arguments]
        return self._wsl("docker", *arguments, target=target, project_cwd=True)

    def _wsl(
        self,
        *arguments: str,
        target: DockerTargetRequest | StackRequest | AcceptanceRequest,
        project_cwd: bool = False,
    ) -> list[str]:
        selected = self._target(target)
        if selected["transport"] != "wsl2":
            raise ValueError("WSL command requires the wsl2 Docker transport")
        command = ["wsl.exe"]
        if selected["wsl_distribution"]:
            command.extend(["--distribution", selected["wsl_distribution"]])
        if project_cwd:
            command.extend(["--cd", str(self.project_root)])
        command.extend(["--exec", *arguments])
        return command

    def target_details(
        self, request: DockerTargetRequest | StackRequest | AcceptanceRequest | None
    ) -> dict[str, str]:
        selected = self._target(request)
        label = {
            "local": "本机 Docker CLI",
            "context": f"Docker context: {selected['docker_context']}",
            "wsl2": (
                f"WSL2: {selected['wsl_distribution']}"
                if selected["wsl_distribution"]
                else "WSL2: 默认发行版"
            ),
        }[selected["transport"]]
        return {**selected, "label": label}

    async def _command(
        self,
        operation: ConsoleOperation,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        effective_env = dict(env or {})
        if command and Path(command[0]).name.lower() == "wsl.exe":
            forwarded = {
                key
                for key in WSL_COMPOSE_ENV_KEYS
                if key in effective_env or key in os.environ
            }
            inherited = [item for item in os.getenv("WSLENV", "").split(":") if item]
            inherited_names = {item.partition("/")[0] for item in inherited}
            effective_env["WSLENV"] = ":".join(
                [*inherited, *sorted(forwarded - inherited_names)]
            )
        return await self.command_runner.run(
            command,
            cwd=self.project_root,
            env=effective_env or None,
            emit=operation.add_log,
        )

    async def _step(
        self,
        operation: ConsoleOperation,
        name: str,
        work: Awaitable[Any],
    ) -> Any:
        step = {"name": name, "status": "running", "started_at": _now().isoformat()}
        operation.steps.append(step)
        operation.add_log("step", name)
        try:
            result = await work
        except Exception:
            step["status"] = "failed"
            step["finished_at"] = _now().isoformat()
            raise
        step["status"] = "succeeded"
        step["finished_at"] = _now().isoformat()
        return result

    async def _verify_docker(
        self,
        operation: ConsoleOperation,
        target: DockerTargetRequest | StackRequest | AcceptanceRequest,
    ) -> dict[str, str]:
        version = await self._command(
            operation,
            self._docker("version", "--format", "{{.Server.Version}}", target=target),
        )
        compose = await self._command(
            operation,
            self._docker("compose", "version", "--short", target=target),
        )
        return {
            "docker_version": next((line for line in version.stdout if line.strip()), "unknown"),
            "compose_version": next((line for line in compose.stdout if line.strip()), "unknown"),
        }

    async def _build_images(
        self, operation: ConsoleOperation, request: StackRequest, environment: dict[str, str]
    ) -> None:
        if self._target(request)["transport"] != "wsl2":
            await self._command(
                operation,
                self._docker("compose", "build", target=request),
                env=environment,
            )
            return

        staging_root = f"/tmp/agentsupport-build-{operation.id}"
        archive = f"{staging_root}.tar"
        try:
            await self._command(
                operation,
                self._wsl(
                    "tar",
                    "--create",
                    "--file",
                    archive,
                    *(f"--exclude={pattern}" for pattern in WSL_BUILD_EXCLUDES),
                    *WSL_BUILD_SOURCES,
                    target=request,
                    project_cwd=True,
                ),
            )
            await self._command(
                operation,
                self._wsl("mkdir", "--parents", staging_root, target=request),
            )
            await self._command(
                operation,
                self._wsl(
                    "tar",
                    "--extract",
                    "--file",
                    archive,
                    "--directory",
                    staging_root,
                    target=request,
                ),
            )
            await self._command(
                operation,
                self._docker("build", "--tag", "agentsupport-api", staging_root, target=request),
                env=environment,
            )
        finally:
            try:
                await self._command(
                    operation,
                    self._wsl(
                        "rm",
                        "--recursive",
                        "--force",
                        "--",
                        staging_root,
                        archive,
                        target=request,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - cleanup must not hide build failures
                operation.add_log("warning", f"清理 WSL 构建暂存目录失败: {exc}")

    async def start(self, action: str, payload: BaseModel | None = None) -> ConsoleOperation:
        async with self._lock:
            if self._active_id:
                active = self.operations.get(self._active_id)
                if active and active.status in {"queued", "running"}:
                    raise RuntimeError(f"operation {active.id} is still running")
            operation = ConsoleOperation(id=uuid4().hex, action=action)
            self.operations[operation.id] = operation
            self._active_id = operation.id
            task = asyncio.create_task(self._run(operation, payload))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return operation

    async def _run(self, operation: ConsoleOperation, payload: BaseModel | None) -> None:
        operation.status = "running"
        operation.started_at = _now()
        try:
            if operation.action in {"deploy", "start"}:
                assert isinstance(payload, StackRequest)
                await self._start_stack(operation, payload)
            elif operation.action == "stop":
                assert isinstance(payload, DockerTargetRequest)
                await self._stop_stack(operation, payload)
            elif operation.action == "accept":
                assert isinstance(payload, AcceptanceRequest)
                await self._accept(operation, payload)
            elif operation.action == "api-accept":
                assert isinstance(payload, ApiAcceptanceRequest)
                await self._api_accept(operation, payload)
            else:
                raise RuntimeError(f"unsupported operation: {operation.action}")
        except Exception as exc:  # noqa: BLE001 - operation failures are returned to the UI
            operation.status = "failed"
            operation.error = str(exc)
            operation.add_log("error", str(exc))
        else:
            operation.status = "succeeded"
        finally:
            if operation.action == "accept":
                shutil.rmtree(
                    Path(tempfile.gettempdir())
                    / "agentsupport-console"
                    / f"console-{operation.id}",
                    ignore_errors=True,
                )
            operation.finished_at = _now()
            async with self._lock:
                if self._active_id == operation.id:
                    self._active_id = None
            if self.on_operation_finished:
                self.on_operation_finished(operation)

    async def _start_stack(self, operation: ConsoleOperation, request: StackRequest) -> None:
        environment = {"SESSION_RUNNER_MODE": request.runner_mode}
        connection = await self._step(
            operation, "检查 Docker 与 Compose", self._verify_docker(operation, request)
        )
        if operation.action == "deploy" and request.rebuild:
            await self._step(
                operation,
                "构建服务镜像",
                self._build_images(operation, request, environment),
            )
        await self._step(
            operation,
            "启动分布式服务",
            self._command(
                operation,
                self._docker(
                    "compose",
                    "up",
                    "-d",
                    "--remove-orphans",
                    "--scale",
                    f"api={request.api_replicas}",
                    "--scale",
                    f"worker={request.worker_replicas}",
                    target=request,
                ),
                env=environment,
            ),
        )
        ready = await self._step(operation, "等待 API 就绪", self._wait_ready())
        lan_forward = await self._step(
            operation, "开放局域网访问（Windows 端口转发）", self._lan_forward(operation, request)
        )
        if lan_forward["status"] != "ok":
            detail = lan_forward.get("reason") or lan_forward.get("error") or lan_forward["status"]
            operation.add_log("info", f"开放局域网访问未执行: {detail}")
        operation.result = {
            "ready": ready,
            "api_replicas": request.api_replicas,
            "worker_replicas": request.worker_replicas,
            "runner_mode": request.runner_mode,
            "connection": {**self.target_details(request), **connection},
            "lan_forward": lan_forward,
        }

    async def _lan_forward(
        self, operation: ConsoleOperation, request: StackRequest
    ) -> dict[str, Any]:
        """Publish the WSL2-hosted API to the LAN via a Windows portproxy + firewall rule.

        This is a convenience step: it runs only on a Windows host with the wsl2 Docker
        transport and never fails the deployment (a declined UAC prompt or a missing
        script only produces a warning).
        """
        if os.name != "nt":
            return {"status": "skipped", "reason": "非 Windows 主机，无需端口转发"}
        if self._target(request)["transport"] != "wsl2":
            return {"status": "skipped", "reason": "Docker transport 不是 wsl2，端口已按所选 transport 暴露"}
        if (
            os.getenv("AGENTSUPPORT_DEV_LAN_FORWARD", "auto").strip().lower()
            in LAN_FORWARD_DISABLED
        ):
            return {"status": "skipped", "reason": "AGENTSUPPORT_DEV_LAN_FORWARD 已关闭"}
        script = self.project_root / LAN_FORWARD_SCRIPT
        if not script.is_file():
            return {"status": "skipped", "reason": f"{script.name} 不存在"}
        command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ]
        distribution = (request.wsl_distribution or self.wsl_distribution or "").strip()
        if distribution:
            command += ["-WslDistro", distribution]
        try:
            await self._command(operation, command)
        except Exception as exc:  # noqa: BLE001 - LAN exposure must not fail the deployment
            operation.add_log("warning", f"开放局域网访问未生效（不影响部署结果）: {exc}")
            return {"status": "failed", "error": str(exc)}
        return {"status": "ok"}

    async def _stop_stack(
        self, operation: ConsoleOperation, request: DockerTargetRequest
    ) -> None:
        connection = await self._step(
            operation, "检查 Docker 与 Compose", self._verify_docker(operation, request)
        )
        await self._step(
            operation,
            "停止服务并保留数据卷",
            self._command(operation, self._docker("compose", "stop", target=request)),
        )
        operation.result = {
            "stopped": True,
            "volumes_preserved": True,
            "connection": {**self.target_details(request), **connection},
        }

    async def _wait_ready(self, timeout: float = 90) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        last_error = "API has not responded"
        async with httpx.AsyncClient(
            base_url=self.agentsupport_url,
            timeout=5,
            transport=self.agentsupport_transport,
        ) as client:
            while asyncio.get_running_loop().time() < deadline:
                try:
                    response = await client.get("/ready")
                    response.raise_for_status()
                    payload = response.json()
                    if payload.get("status") == "ready":
                        return payload
                except Exception as exc:  # noqa: BLE001 - readiness is retried until timeout
                    last_error = str(exc)
                await asyncio.sleep(1)
        raise TimeoutError(f"API readiness timed out: {last_error}")

    async def _accept(
        self, operation: ConsoleOperation, request: AcceptanceRequest
    ) -> None:
        pytest_root = (
            Path(tempfile.gettempdir()) / "agentsupport-console" / f"console-{operation.id}"
        )
        pytest_root.mkdir(parents=True, exist_ok=True)
        temp_root = str(pytest_root)
        python = sys.executable
        connection = await self._step(
            operation, "检查 Docker 与 Compose", self._verify_docker(operation, request)
        )
        await self._step(
            operation,
            "校验 Compose 配置",
            self._command(
                operation,
                self._docker("compose", "config", "--quiet", target=request),
            ),
        )
        await self._step(
            operation,
            "运行 Ruff 静态检查",
            self._command(
                operation,
                [python, "-m", "ruff", "check", "src", "tests", "alembic", "devtools"],
            ),
        )
        await self._step(
            operation,
            "运行回归测试",
            self._command(
                operation,
                [
                    python,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    os.path.join(temp_root, "fast"),
                ],
                env={"PYTHONUTF8": "1"},
            ),
        )
        if request.include_postgres:
            await self._step(
                operation,
                "迁移 PostgreSQL schema",
                self._command(
                    operation,
                    [python, "-m", "alembic", "upgrade", "head"],
                    env={"PYTHONUTF8": "1"},
                ),
            )
            await self._step(
                operation,
                "运行 PostgreSQL 集成测试",
                self._command(
                    operation,
                    [
                        python,
                        "-m",
                        "pytest",
                        "tests/integration/persistence/test_repository_postgres.py",
                        "-q",
                        "-p",
                        "no:cacheprovider",
                        os.path.join(temp_root, "postgres"),
                    ],
                    env={"PYTHONUTF8": "1", "RUN_POSTGRES_INTEGRATION_TESTS": "1"},
                ),
            )
        live_result = await self._step(
            operation,
            "验证运行中的分布式服务",
            self._live_acceptance(request, operation),
        )
        operation.result = {
            **live_result,
            "connection": {**self.target_details(request), **connection},
        }

    async def _live_acceptance(
        self, request: AcceptanceRequest, operation: ConsoleOperation
    ) -> dict[str, Any]:
        await self._wait_ready(timeout=30)
        async with httpx.AsyncClient(
            base_url=self.agentsupport_url,
            timeout=10,
            transport=self.agentsupport_transport,
        ) as client:
            instances: set[str] = set()
            for _ in range(24):
                response = await client.get("/live")
                response.raise_for_status()
                if instance := response.headers.get("X-AgentSupport-Instance"):
                    instances.add(instance)
            expected = min(2, request.expected_api_replicas)
            if len(instances) < expected:
                raise RuntimeError(
                    f"expected {expected} API instance(s), observed {len(instances)}"
                )
            metrics_response = await client.get("/metrics")
            metrics_response.raise_for_status()
            required_metrics = {
                "agentsupport_queue_ready",
                "agentsupport_active_runtimes",
                "agentsupport_claims_expired",
                "agentsupport_outbox_pending",
            }
            metric_names = {
                line.split()[0]
                for line in metrics_response.text.splitlines()
                if line.strip()
            }
            missing = sorted(required_metrics - metric_names)
            if missing:
                raise RuntimeError(f"missing metrics: {', '.join(missing)}")

            conversation_id: str | None = None
            event_types: list[str] = []
            if request.include_task_smoke:
                suffix = operation.id[:10]
                workspace = await client.post(
                    "/workspaces",
                    headers={"Idempotency-Key": f"console-workspace-{operation.id}"},
                    json={"name": f"console-accept-{suffix}"},
                )
                workspace.raise_for_status()
                session = await client.post(
                    "/sessions",
                    headers={"Idempotency-Key": f"console-session-{operation.id}"},
                    json={"workspace_id": workspace.json()["id"]},
                )
                session.raise_for_status()
                conversation = await client.post(
                    f"/sessions/{session.json()['id']}/conversations",
                    headers={"Idempotency-Key": f"console-conversation-{operation.id}"},
                    json={"task": f"console-acceptance-{suffix}"},
                )
                conversation.raise_for_status()
                conversation_id = conversation.json()["id"]
                deadline = asyncio.get_running_loop().time() + 45
                while asyncio.get_running_loop().time() < deadline:
                    events = await client.get(f"/conversations/{conversation_id}/events")
                    events.raise_for_status()
                    event_types = [event["type"] for event in events.json()]
                    if "run.completed" in event_types:
                        break
                    if "run.failed" in event_types:
                        raise RuntimeError("live acceptance Run failed")
                    await asyncio.sleep(0.5)
                if "run.completed" not in event_types:
                    raise TimeoutError("live acceptance Run did not complete")

        operation.add_log("result", f"API instances: {', '.join(sorted(instances))}")
        if conversation_id:
            operation.add_log("result", f"Conversation {conversation_id} completed")
        return {
            "api_instances": sorted(instances),
            "metrics": sorted(required_metrics),
            "conversation_id": conversation_id,
            "event_types": event_types,
        }

    async def _api_accept(
        self, operation: ConsoleOperation, request: ApiAcceptanceRequest
    ) -> None:
        await self._step(operation, "等待 API 就绪", self._wait_ready(timeout=30))
        verifier = ApiContractVerifier(
            base_url=self.agentsupport_url,
            transport=self.agentsupport_transport,
            timeout=request.timeout_seconds,
            suffix=operation.id[:10],
        )

        def emit(stream: str, message: str) -> None:
            operation.add_log(stream, message)

        report = await self._step(
            operation,
            "执行 API 契约验收",
            verifier.run(
                include_negative=request.include_negative,
                include_sse=request.include_sse,
                include_task_flow=request.include_task_flow,
                emit=emit,
            ),
        )
        operation.result = report

    def operation(self, operation_id: str) -> ConsoleOperation | None:
        return self.operations.get(operation_id)

    async def deployment_environment(
        self, request: DockerTargetRequest
    ) -> dict[str, Any]:
        selected = self.target_details(request)
        distributions: list[str] = []
        wsl_error: str | None = None
        if os.name == "nt":
            try:
                listed = await self.command_runner.run(
                    ["wsl.exe", "--list", "--quiet"],
                    cwd=self.project_root,
                    env=None,
                    emit=lambda _stream, _message: None,
                )
                distributions = [
                    line.replace("\x00", "").replace("\ufeff", "").strip()
                    for line in listed.stdout
                    if line.replace("\x00", "").replace("\ufeff", "").strip()
                ]
            except Exception as exc:  # noqa: BLE001 - diagnostics report unavailable tools
                wsl_error = str(exc)

        try:
            version = await self.command_runner.run(
                self._docker("version", "--format", "{{.Server.Version}}", target=request),
                cwd=self.project_root,
                env=None,
                emit=lambda _stream, _message: None,
            )
            compose = await self.command_runner.run(
                self._docker("compose", "version", "--short", target=request),
                cwd=self.project_root,
                env=None,
                emit=lambda _stream, _message: None,
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics are returned to the UI
            result: dict[str, Any] = {
                "available": False,
                "target": selected,
                "distributions": distributions,
                "error": str(exc),
            }
            if wsl_error:
                result["wsl_error"] = wsl_error
            return result

        return {
            "available": True,
            "target": selected,
            "distributions": distributions,
            "docker_version": next((line for line in version.stdout if line.strip()), "unknown"),
            "compose_version": next((line for line in compose.stdout if line.strip()), "unknown"),
            **({"wsl_error": wsl_error} if wsl_error else {}),
        }

    async def stack_status(
        self, request: DockerTargetRequest | None = None
    ) -> dict[str, Any]:
        result = await self.command_runner.run(
            self._docker("compose", "ps", "--format", "json", target=request),
            cwd=self.project_root,
            env=None,
            emit=lambda _stream, _message: None,
        )
        raw = "\n".join(result.stdout).strip()
        records: list[dict[str, Any]] = []
        if raw:
            try:
                parsed = json.loads(raw)
                records = parsed if isinstance(parsed, list) else [parsed]
            except json.JSONDecodeError:
                records = [json.loads(line) for line in result.stdout if line.strip()]
        services = [
            {
                "name": item.get("Name", ""),
                "service": item.get("Service", ""),
                "state": item.get("State", "unknown"),
                "health": item.get("Health", ""),
                "status": item.get("Status", ""),
            }
            for item in records
        ]
        ready: dict[str, Any] | None = None
        metrics: dict[str, int] = {}
        dependency_error: str | None = None
        try:
            async with httpx.AsyncClient(
                base_url=self.agentsupport_url,
                timeout=2,
                transport=self.agentsupport_transport,
            ) as client:
                ready_response = await client.get("/ready")
                if ready_response.is_success:
                    ready = ready_response.json()
                metrics_response = await client.get("/metrics")
                if metrics_response.is_success:
                    for line in metrics_response.text.splitlines():
                        name, _, value = line.partition(" ")
                        if name and value:
                            metrics[name.removeprefix("agentsupport_")] = int(float(value))
        except Exception as exc:  # noqa: BLE001 - stopped stack is a valid status
            dependency_error = str(exc)
        result_payload = {
            "services": services,
            "ready": ready,
            "metrics": metrics,
            "target": self.target_details(request),
        }
        if dependency_error:
            result_payload["error"] = dependency_error
        return result_payload


def create_dev_console_app(
    controller: DevConsoleController | None = None,
    *,
    token: str | None = None,
    monitor_log_dir: Path | None = None,
) -> FastAPI:
    selected = controller or DevConsoleController()
    console_token = token or secrets.token_urlsafe(32)
    monitor = EnvironmentMonitor(selected, log_dir=monitor_log_dir)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await monitor.start()
        monitor.record_event(
            "console_start",
            "info",
            f"Dev console started (pid={os.getpid()})",
        )
        try:
            yield
        finally:
            await monitor.stop()

    app = FastAPI(
        title="AgentSupport Dev Console",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.controller = selected
    app.state.console_token = console_token
    app.state.monitor = monitor
    selected.on_operation_finished = monitor.handle_operation_finished

    def authorize(x_dev_console_token: str | None = Header(default=None)) -> None:
        if not x_dev_console_token or not secrets.compare_digest(
            x_dev_console_token, console_token
        ):
            raise HTTPException(403, "invalid development console token")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        html = (CONTROL_UI_ROOT / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__DEV_CONSOLE_TOKEN__", console_token))

    @app.api_route(
        "/agentsupport/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def agentsupport_proxy(path: str, request: Request):
        client = httpx.AsyncClient(
            base_url=selected.agentsupport_url,
            timeout=None,
            transport=selected.agentsupport_transport,
        )
        request_headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower() not in HOP_BY_HOP_HEADERS
            and name.lower() not in {"host", "content-length", "x-dev-console-token"}
        }
        upstream_request = client.build_request(
            request.method,
            f"/{path}",
            params=request.query_params,
            headers=request_headers,
            content=await request.body(),
        )
        try:
            upstream = await client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            raise HTTPException(502, f"AgentSupport API is unavailable: {exc}") from exc
        response_headers = {
            name: value
            for name, value in upstream.headers.items()
            if name.lower() not in HOP_BY_HOP_HEADERS
            and name.lower() not in {"content-length"}
        }
        if upstream.headers.get("content-type", "").startswith("text/event-stream"):

            async def stream_body():
                try:
                    async for chunk in upstream.aiter_raw():
                        yield chunk
                except httpx.RemoteProtocolError:
                    # The upstream SSE connection can end without a complete
                    # chunked body (for example when its event producer
                    # terminates or a proxy closes the connection). Treat that
                    # as a normal end-of-stream instead of letting the protocol
                    # error bubble up as an ASGI exception.
                    return
                finally:
                    await upstream.aclose()
                    await client.aclose()

            return StreamingResponse(
                stream_body(),
                status_code=upstream.status_code,
                headers=response_headers,
                media_type="text/event-stream",
            )
        content = await upstream.aread()
        await upstream.aclose()
        await client.aclose()
        return Response(
            content=content,
            status_code=upstream.status_code,
            headers=response_headers,
        )

    @app.get("/api/status")
    async def status(
        docker_transport: Literal["auto", "local", "context", "wsl2"] = "auto",
        docker_context: str = "",
        wsl_distribution: str = "",
        x_dev_console_token: str | None = Header(default=None),
    ):
        authorize(x_dev_console_token)
        target = DockerTargetRequest(
            docker_transport=docker_transport,
            docker_context=docker_context,
            wsl_distribution=wsl_distribution,
        )
        try:
            target_details = selected.target_details(target)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        try:
            return await selected.stack_status(target)
        except Exception as exc:  # noqa: BLE001 - Docker unavailable is rendered as status
            return {
                "services": [],
                "ready": None,
                "metrics": {},
                "target": target_details,
                "error": str(exc),
            }

    @app.get("/api/environment")
    async def environment(
        docker_transport: Literal["auto", "local", "context", "wsl2"] = "auto",
        docker_context: str = "",
        wsl_distribution: str = "",
        x_dev_console_token: str | None = Header(default=None),
    ):
        authorize(x_dev_console_token)
        target = DockerTargetRequest(
            docker_transport=docker_transport,
            docker_context=docker_context,
            wsl_distribution=wsl_distribution,
        )
        try:
            return await selected.deployment_environment(target)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/operations/{operation_id}")
    async def operation(
        operation_id: str,
        x_dev_console_token: str | None = Header(default=None),
    ):
        authorize(x_dev_console_token)
        item = selected.operation(operation_id)
        if item is None:
            raise HTTPException(404, "operation does not exist")
        return item.snapshot()

    async def begin(action: str, payload: BaseModel | None) -> dict[str, Any]:
        try:
            item = await selected.start(action, payload)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return item.snapshot()

    @app.post("/api/actions/deploy", status_code=202)
    async def deploy(
        payload: StackRequest,
        x_dev_console_token: str | None = Header(default=None),
    ):
        authorize(x_dev_console_token)
        return await begin("deploy", payload)

    @app.post("/api/actions/start", status_code=202)
    async def start(
        payload: StackRequest,
        x_dev_console_token: str | None = Header(default=None),
    ):
        authorize(x_dev_console_token)
        return await begin("start", payload)

    @app.post("/api/actions/stop", status_code=202)
    async def stop(
        payload: DockerTargetRequest | None = None,
        x_dev_console_token: str | None = Header(default=None),
    ):
        authorize(x_dev_console_token)
        return await begin("stop", payload or DockerTargetRequest())

    @app.post("/api/actions/accept", status_code=202)
    async def accept(
        payload: AcceptanceRequest,
        x_dev_console_token: str | None = Header(default=None),
    ):
        authorize(x_dev_console_token)
        return await begin("accept", payload)

    @app.post("/api/actions/api-accept", status_code=202)
    async def api_accept(
        payload: ApiAcceptanceRequest,
        x_dev_console_token: str | None = Header(default=None),
    ):
        authorize(x_dev_console_token)
        return await begin("api-accept", payload)

    @app.get("/api/monitor/overview")
    async def monitor_overview(
        x_dev_console_token: str | None = Header(default=None),
    ):
        authorize(x_dev_console_token)
        return monitor.overview()

    @app.get("/api/monitor/events")
    async def monitor_events(
        limit: int = 300,
        event_type: str | None = None,
        include_snapshots: bool = False,
        x_dev_console_token: str | None = Header(default=None),
    ):
        authorize(x_dev_console_token)
        if not include_snapshots and event_type is None:
            event_type = "-snapshot"
        return {
            "counts": dict(monitor._counts),
            "events": monitor.event_list(limit=limit, event_type=event_type),
        }

    @app.get("/api/monitor/logs")
    async def monitor_logs(
        service: str,
        tail: int = 200,
        x_dev_console_token: str | None = Header(default=None),
    ):
        authorize(x_dev_console_token)
        return await monitor.container_logs(service=service, tail=tail)

    @app.get("/api/monitor/journal")
    async def monitor_journal(
        lines: int = 150,
        kind: str = "wsl",
        x_dev_console_token: str | None = Header(default=None),
    ):
        authorize(x_dev_console_token)
        return await monitor.journal_tail(lines=lines, kind=kind)

    @app.get("/api/monitor/console")
    async def monitor_console(
        lines: int = 120,
        x_dev_console_token: str | None = Header(default=None),
    ):
        authorize(x_dev_console_token)
        return monitor.console_logs(lines=lines)

    app.mount("/assets", StaticFiles(directory=CONTROL_UI_ROOT), name="control-ui-assets")

    @app.get("/tasks", response_class=HTMLResponse)
    @app.get("/tasks/", response_class=HTMLResponse)
    async def tasks_index() -> HTMLResponse:
        html = (CONTROL_UI_ROOT / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__DEV_CONSOLE_TOKEN__", console_token))

    return app


app = create_dev_console_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="AgentSupport local deployment console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    arguments = parser.parse_args()
    if arguments.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("the development console may only bind to a loopback address")
    import uvicorn

    uvicorn.run(app, host=arguments.host, port=arguments.port)


if __name__ == "__main__":
    main()

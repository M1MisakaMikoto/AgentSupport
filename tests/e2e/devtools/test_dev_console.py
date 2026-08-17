import asyncio
import os
import re
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient

from devtools.console.app import (
    AcceptanceRequest,
    CommandResult,
    ConsoleOperation,
    DevConsoleController,
    DockerTargetRequest,
    StackRequest,
    create_dev_console_app,
)


class FakeCommandRunner:
    def __init__(self):
        self.calls = []

    async def run(self, command, *, cwd, env, emit):
        self.calls.append({"command": command, "cwd": cwd, "env": env})
        emit("command", " ".join(command))
        if command[-4:] == ["compose", "ps", "--format", "json"]:
            return CommandResult(
                0,
                stdout=[
                    (
                        '{"Name":"agent-api-1","Service":"api","State":"running",'
                        '"Health":"healthy","Status":"Up"}'
                    )
                ],
            )
        return CommandResult(0, stdout=["ok"])


async def _wait_for_operation(client, operation_id, token):
    for _ in range(600):
        response = await client.get(
            f"/api/operations/{operation_id}",
            headers={"X-Dev-Console-Token": token},
        )
        if response.json()["status"] in {"succeeded", "failed"}:
            return response.json()
        await asyncio.sleep(0.02)
    raise AssertionError("operation did not finish")


@pytest.mark.asyncio
async def test_dev_console_requires_token_and_runs_fixed_deploy_commands(monkeypatch, tmp_path):
    runner = FakeCommandRunner()
    controller = DevConsoleController(command_runner=runner, docker_transport="local")

    async def ready():
        return {"status": "ready"}

    monkeypatch.setattr(controller, "_wait_ready", ready)
    app = create_dev_console_app(controller, token="test-token", monitor_log_dir=tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        index = await client.get("/")
        denied = await client.post("/api/actions/deploy", json={})
        accepted = await client.post(
            "/api/actions/deploy",
            headers={"X-Dev-Console-Token": "test-token"},
            json={
                "api_replicas": 2,
                "worker_replicas": 3,
                "runner_mode": "deterministic",
                "rebuild": True,
            },
        )
        operation = await _wait_for_operation(client, accepted.json()["id"], "test-token")

    assert index.status_code == 200
    assert "test-token" in index.text
    assert "执行回归验收" in index.text
    assert "执行完整验收" not in index.text
    assert denied.status_code == 403
    assert accepted.status_code == 202
    assert operation["status"] == "succeeded"
    commands = [call["command"] for call in runner.calls]
    assert any(command[-3:] == ["compose", "version", "--short"] for command in commands)
    assert any(command[-2:] == ["compose", "build"] for command in commands)
    up_call = next(call for call in runner.calls if "up" in call["command"])
    assert up_call["command"][-4:] == [
        "--scale",
        "api=2",
        "--scale",
        "worker=3",
    ]
    assert up_call["env"] == {"SESSION_RUNNER_MODE": "deterministic"}


@pytest.mark.asyncio
async def test_dev_console_rejects_out_of_range_replica_counts():
    app = create_dev_console_app(DevConsoleController(command_runner=FakeCommandRunner()), token="t")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/actions/start",
            headers={"X-Dev-Console-Token": "t"},
            json={"api_replicas": 99, "worker_replicas": 3},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_stop_without_body_uses_default_docker_target(tmp_path):
    runner = FakeCommandRunner()
    controller = DevConsoleController(command_runner=runner, docker_transport="local")
    app = create_dev_console_app(controller, token="stop-token", monitor_log_dir=tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/actions/stop",
            headers={"X-Dev-Console-Token": "stop-token"},
        )
        operation = await _wait_for_operation(client, response.json()["id"], "stop-token")

    assert response.status_code == 202
    assert operation["status"] == "succeeded"
    assert runner.calls[-1]["command"] == ["docker", "compose", "stop"]


@pytest.mark.asyncio
async def test_start_with_wsl2_transport_runs_lan_forward_step(monkeypatch, tmp_path):
    runner = FakeCommandRunner()
    controller = DevConsoleController(command_runner=runner, docker_transport="wsl2")
    monkeypatch.setattr(os, "name", "nt")

    async def ready():
        return {"status": "ready"}

    monkeypatch.setattr(controller, "_wait_ready", ready)
    app = create_dev_console_app(controller, token="lan-token", monitor_log_dir=tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        accepted = await client.post(
            "/api/actions/start",
            headers={"X-Dev-Console-Token": "lan-token"},
            json={"api_replicas": 2, "worker_replicas": 3, "runner_mode": "deterministic"},
        )
        operation = await _wait_for_operation(client, accepted.json()["id"], "lan-token")

    assert accepted.status_code == 202
    assert operation["status"] == "succeeded"
    assert operation["result"]["lan_forward"]["status"] == "ok"
    commands = [call["command"] for call in runner.calls]
    assert any(
        command[0] == "powershell.exe"
        and Path(command[-1]).name == "wsl-lan-forward.ps1"
        for command in commands
    )


@pytest.mark.asyncio
async def test_start_with_local_transport_skips_lan_forward(monkeypatch, tmp_path):
    runner = FakeCommandRunner()
    controller = DevConsoleController(command_runner=runner, docker_transport="local")
    monkeypatch.setattr(os, "name", "nt")

    async def ready():
        return {"status": "ready"}

    monkeypatch.setattr(controller, "_wait_ready", ready)
    app = create_dev_console_app(controller, token="local-token", monitor_log_dir=tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        accepted = await client.post(
            "/api/actions/start",
            headers={"X-Dev-Console-Token": "local-token"},
            json={"api_replicas": 2, "worker_replicas": 3, "runner_mode": "deterministic"},
        )
        operation = await _wait_for_operation(client, accepted.json()["id"], "local-token")

    assert operation["status"] == "succeeded"
    assert operation["result"]["lan_forward"]["status"] == "skipped"
    commands = [call["command"] for call in runner.calls]
    assert not any(command[0] == "powershell.exe" for command in commands)


@pytest.mark.asyncio
async def test_start_with_lan_forward_disabled_skips_step(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTSUPPORT_DEV_LAN_FORWARD", "off")
    runner = FakeCommandRunner()
    controller = DevConsoleController(command_runner=runner, docker_transport="wsl2")
    monkeypatch.setattr(os, "name", "nt")

    async def ready():
        return {"status": "ready"}

    monkeypatch.setattr(controller, "_wait_ready", ready)
    app = create_dev_console_app(controller, token="off-token", monitor_log_dir=tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        accepted = await client.post(
            "/api/actions/start",
            headers={"X-Dev-Console-Token": "off-token"},
            json={"api_replicas": 2, "worker_replicas": 3, "runner_mode": "deterministic"},
        )
        operation = await _wait_for_operation(client, accepted.json()["id"], "off-token")

    assert operation["status"] == "succeeded"
    assert operation["result"]["lan_forward"]["status"] == "skipped"
    assert operation["result"]["lan_forward"]["reason"] == "AGENTSUPPORT_DEV_LAN_FORWARD 已关闭"
    commands = [call["command"] for call in runner.calls]
    assert not any(command[0] == "powershell.exe" for command in commands)


@pytest.mark.asyncio
async def test_dev_console_status_normalizes_compose_json(monkeypatch):
    runner = FakeCommandRunner()
    controller = DevConsoleController(command_runner=runner)

    class UnavailableClient:
        async def __aenter__(self):
            raise ConnectError()

        async def __aexit__(self, *args):
            return None

    class ConnectError(Exception):
        pass

    monkeypatch.setattr("devtools.console.app.httpx.AsyncClient", lambda **kwargs: UnavailableClient())
    app = create_dev_console_app(controller, token="status-token")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        denied = await client.get("/api/status")
        response = await client.get(
            "/api/status", headers={"X-Dev-Console-Token": "status-token"}
        )
    status = response.json()

    assert denied.status_code == 403
    assert response.status_code == 200
    assert status["services"] == [
        {
            "name": "agent-api-1",
            "service": "api",
            "state": "running",
            "health": "healthy",
            "status": "Up",
        }
    ]
    assert status["ready"] is None


@pytest.mark.asyncio
async def test_monitor_endpoints_require_token_and_serve_logs(tmp_path):
    runner = FakeCommandRunner()
    controller = DevConsoleController(command_runner=runner, docker_transport="local")
    app = create_dev_console_app(
        controller,
        token="monitor-token",
        monitor_log_dir=tmp_path,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        denied = await client.get("/api/monitor/overview")
        overview = await client.get(
            "/api/monitor/overview",
            headers={"X-Dev-Console-Token": "monitor-token"},
        )
        events = await client.get(
            "/api/monitor/events?limit=5",
            headers={"X-Dev-Console-Token": "monitor-token"},
        )
        logs = await client.get(
            "/api/monitor/logs?service=api&tail=50",
            headers={"X-Dev-Console-Token": "monitor-token"},
        )
        journal = await client.get(
            "/api/monitor/journal?lines=20&kind=wsl",
            headers={"X-Dev-Console-Token": "monitor-token"},
        )
        console = await client.get(
            "/api/monitor/console?lines=20",
            headers={"X-Dev-Console-Token": "monitor-token"},
        )

    assert denied.status_code == 403
    assert overview.status_code == 200
    body = overview.json()
    assert body["monitor"]["log_path"].endswith("agentsupport-monitor.jsonl")
    assert body["api"]["url"] == "http://127.0.0.1:8000"
    assert body["wsl"]["available"] is None
    assert events.status_code == 200
    assert events.json()["counts"]["console_start"] == 0
    assert logs.status_code == 200
    assert logs.json()["service"] == "api"
    assert logs.json()["containers"] == ["agent-api-1"]
    assert logs.json()["lines"] == ["[agent-api-1] ok"]
    assert journal.status_code == 200
    assert journal.json()["available"] is False
    assert console.status_code == 200
    assert isinstance(console.json().get("dev-console.out.log"), list)


def test_dev_console_builds_direct_wsl2_docker_command(tmp_path):
    controller = DevConsoleController(
        project_root=tmp_path,
        command_runner=FakeCommandRunner(),
        docker_transport="local",
    )
    target = DockerTargetRequest(
        docker_transport="wsl2",
        wsl_distribution="Ubuntu-24.04",
    )

    command = controller._docker("compose", "ps", target=target)

    assert command == [
        "wsl.exe",
        "--distribution",
        "Ubuntu-24.04",
        "--cd",
        str(tmp_path.resolve()),
        "--exec",
        "docker",
        "compose",
        "ps",
    ]


@pytest.mark.asyncio
async def test_wsl2_build_uses_native_staging_context(tmp_path):
    runner = FakeCommandRunner()
    controller = DevConsoleController(
        project_root=tmp_path,
        command_runner=runner,
        docker_transport="wsl2",
    )
    operation = ConsoleOperation(id="build-operation", action="deploy")
    request = StackRequest(
        docker_transport="wsl2",
        wsl_distribution="Ubuntu",
    )

    await controller._build_images(
        operation,
        request,
        {"SESSION_RUNNER_MODE": "deterministic"},
    )

    commands = [call["command"] for call in runner.calls]
    assert commands[0][:5] == ["wsl.exe", "--distribution", "Ubuntu", "--cd", str(tmp_path)]
    assert "--exclude=.pytest_cache" in commands[0]
    assert "vendor/trae-agent-src" in commands[0]
    docker_build = next(command for command in commands if "build" in command)
    assert docker_build[-4:] == [
        "build",
        "--tag",
        "agentsupport-api",
        "/tmp/agentsupport-build-build-operation",
    ]
    assert not any(command[-2:] == ["compose", "build"] for command in commands)
    assert commands[-1][-5:] == [
        "--recursive",
        "--force",
        "--",
        "/tmp/agentsupport-build-build-operation",
        "/tmp/agentsupport-build-build-operation.tar",
    ]


@pytest.mark.asyncio
async def test_wsl2_commands_forward_only_compose_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("TRAE_API_KEY", "secret")
    monkeypatch.setenv("UNRELATED_VALUE", "not-forwarded")
    runner = FakeCommandRunner()
    controller = DevConsoleController(
        project_root=tmp_path,
        command_runner=runner,
        docker_transport="wsl2",
    )
    operation = ConsoleOperation(id="wsl-env", action="deploy")
    target = DockerTargetRequest(docker_transport="wsl2")

    await controller._command(
        operation,
        controller._docker("compose", "config", target=target),
        env={"SESSION_RUNNER_MODE": "trae"},
    )

    forwarded = runner.calls[0]["env"]["WSLENV"].split(":")
    assert "SESSION_RUNNER_MODE" in forwarded
    assert "TRAE_API_KEY" in forwarded
    assert "UNRELATED_VALUE" not in forwarded
    assert "TRAE_API_KEY" not in runner.calls[0]["env"]


@pytest.mark.asyncio
async def test_environment_diagnostic_reports_unavailable_docker():
    class UnavailableRunner:
        async def run(self, command, *, cwd, env, emit):
            raise FileNotFoundError(command[0])

    controller = DevConsoleController(
        command_runner=UnavailableRunner(),
        docker_transport="wsl2",
    )
    app = create_dev_console_app(controller, token="diagnostic-token")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/environment?docker_transport=wsl2&wsl_distribution=Ubuntu",
            headers={"X-Dev-Console-Token": "diagnostic-token"},
        )

    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["target"]["label"] == "WSL2: Ubuntu"
    assert "wsl.exe" in response.json()["error"]


@pytest.mark.asyncio
async def test_acceptance_operation_uses_controlled_test_commands(monkeypatch, tmp_path):
    runner = FakeCommandRunner()
    controller = DevConsoleController(project_root=tmp_path, command_runner=runner)

    async def live_acceptance(request, operation):
        assert (tmp_path / ".pytest-debug").is_dir()
        return {"api_instances": ["api-a", "api-b"], "conversation_id": "conversation"}

    monkeypatch.setattr(controller, "_live_acceptance", live_acceptance)
    operation = await controller.start(
        "accept",
        AcceptanceRequest(
            expected_api_replicas=2,
            include_postgres=True,
            include_task_smoke=True,
        ),
    )
    for _ in range(50):
        if operation.status in {"succeeded", "failed"}:
            break
        await asyncio.sleep(0.01)

    commands = [call["command"] for call in runner.calls]
    assert operation.status == "succeeded"
    assert not (tmp_path / ".pytest-debug").exists()
    ruff_command = next(command for command in commands if command[2:4] == ["ruff", "check"])
    assert ruff_command[4:] == ["src", "tests", "alembic", "devtools"]
    assert sum("pytest" in command for command in commands) == 2
    postgres_call = next(
        call
        for call in runner.calls
        if "tests/integration/persistence/test_repository.py" in call["command"]
    )
    assert postgres_call["env"] == {
        "PYTHONUTF8": "1",
        "RUN_POSTGRES_DISTRIBUTED_TESTS": "1",
    }
    assert not any(re.search(r"[;&|]", argument) for command in commands for argument in command)


@pytest.mark.asyncio
async def test_one_stop_console_serves_task_ui_and_proxies_api_and_sse():
    agentsupport = FastAPI()

    @agentsupport.get("/live")
    async def live():
        return {"status": "ok"}

    @agentsupport.post("/echo")
    async def echo(request: Request):
        return {
            "body": await request.json(),
            "idempotency_key": request.headers.get("Idempotency-Key"),
            "console_token": request.headers.get("X-Dev-Console-Token"),
        }

    @agentsupport.get("/events")
    async def events():
        async def body():
            yield "id: 1\ndata: {\"type\":\"ready\"}\n\n"

        return StreamingResponse(body(), media_type="text/event-stream")

    controller = DevConsoleController(
        command_runner=FakeCommandRunner(),
        agentsupport_url="http://agentsupport.test",
        agentsupport_transport=ASGITransport(app=agentsupport),
    )
    app = create_dev_console_app(controller, token="proxy-token")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://console") as client:
        task_ui = await client.get("/tasks/")
        task_script = await client.get("/assets/app.js")
        live_response = await client.get("/agentsupport/live")
        echo_response = await client.post(
            "/agentsupport/echo",
            headers={
                "Idempotency-Key": "proxy-once",
                "X-Dev-Console-Token": "must-not-forward",
            },
            json={"value": "forwarded"},
        )
        event_response = await client.get("/agentsupport/events")

    assert task_ui.status_code == 200
    assert "示范" in task_ui.text
    assert "部署" in task_ui.text
    assert "Agent 工作台" in task_ui.text
    assert "API 参考" in task_ui.text
    assert 'data-view="demo-overview"' in task_ui.text
    assert 'data-view="deploy-api"' in task_ui.text
    assert 'id="agent-skills-list"' in task_ui.text
    assert 'id="agent-mcp-list"' in task_ui.text
    assert 'id="agent-run-error"' in task_ui.text
    assert 'apiBase: "/agentsupport"' in task_script.text
    assert live_response.json() == {"status": "ok"}
    assert echo_response.json() == {
        "body": {"value": "forwarded"},
        "idempotency_key": "proxy-once",
        "console_token": None,
    }
    assert event_response.headers["content-type"].startswith("text/event-stream")
    assert 'data: {"type":"ready"}' in event_response.text


@pytest.mark.asyncio
async def test_console_proxy_ends_sse_gracefully_when_upstream_disconnects():
    class BrokenSSETransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            class BrokenStream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b"id: 1\ndata: {\"type\":\"partial\"}\n\n"
                    raise httpx.RemoteProtocolError(
                        "peer closed connection without sending complete message body",
                        request=request,
                    )

                async def aclose(self):
                    return None

            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=BrokenStream(),
            )

    controller = DevConsoleController(
        command_runner=FakeCommandRunner(),
        agentsupport_url="http://agentsupport.test",
        agentsupport_transport=BrokenSSETransport(),
    )
    app = create_dev_console_app(controller, token="proxy-token")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://console"
    ) as client:
        response = await client.get("/agentsupport/events")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert 'data: {"type":"partial"}' in response.text


def test_windows_launcher_starts_loopback_console_without_starting_stack():
    root = Path(__file__).resolve().parents[3]
    powershell = (root / "start-console.ps1").read_text(encoding="utf-8")
    command = (root / "start-console.cmd").read_text(encoding="utf-8")

    assert "devtools.console" in powershell
    assert "http://127.0.0.1:8010/" in powershell
    assert "compose" not in powershell.lower()
    assert "start-console.ps1" in command


@pytest.mark.asyncio
async def test_console_serves_two_section_ui_and_all_assets():
    controller = DevConsoleController(command_runner=FakeCommandRunner())
    console = create_dev_console_app(controller, token="views-token")
    async with AsyncClient(
        transport=ASGITransport(app=console), base_url="http://console"
    ) as client:
        index = await client.get("/")
        assets = ["app.js", "demo.js", "api.js", "ops.js", "styles.css"]
        asset_responses = {
            name: await client.get(f"/assets/{name}") for name in assets
        }
        tasks_page = await client.get("/tasks/")

    assert index.status_code == 200
    for name, response in asset_responses.items():
        assert response.status_code == 200, name
    for marker in (
        'id="view-demo-overview"',
        'id="view-demo-agent"',
        'id="view-deploy-status"',
        'id="view-deploy-actions"',
        'id="view-deploy-acceptance"',
        'id="view-deploy-api"',
        'id="op-dock"',
        'id="modal-mask"',
        "data-primary=\"demo\"",
        "data-primary=\"deploy\"",
    ):
        assert marker in index.text, marker
    assert tasks_page.status_code == 200
    assert "示范" in tasks_page.text


def _fake_agentsupport_app() -> FastAPI:
    """Minimal in-memory AgentSupport API implementing the public contract."""
    import json as _json
    from uuid import UUID as _UUID
    from uuid import uuid4 as _uuid4

    from fastapi import HTTPException, Query, Request
    from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

    instance = "fake-instance-1"
    state = {
        "workspaces": {},
        "sessions": {},
        "orgs": {},
        "users": {},
        "presets": {},
        "projects": {},
        "conversations": {},
        "events": {},
        "idempotency": {},
    }
    now = "2026-08-05T08:00:00Z"

    app = FastAPI()

    @app.middleware("http")
    async def add_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = (
            request.headers.get("X-Correlation-ID") or str(_uuid4())
        )
        response.headers["X-AgentSupport-Instance"] = instance
        return response

    def error(code: str, message: str, status: int) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content={
                "code": code,
                "message": message,
                "retryable": False,
                "operation": "",
                "correlation_id": "",
                "details": None,
            },
        )

    def idempotent(scope: str, key: str | None, payload: dict, create):
        if not key:
            return create(), True
        record = state["idempotency"].get((scope, key))
        digest = _json.dumps(payload, sort_keys=True, ensure_ascii=False)
        if record:
            if record["digest"] != digest:
                return error(
                    "IDEMPOTENCY_CONFLICT",
                    "idempotency key was reused with a different request",
                    409,
                ), True
            return record["value"], False
        value = create()
        state["idempotency"][(scope, key)] = {"digest": digest, "value": value}
        return value, True

    def idempotent_created(scope: str, key: str | None, payload: dict, create):
        value, created = idempotent(scope, key, payload, create)
        if isinstance(value, JSONResponse):
            return value
        if created:
            return value
        return JSONResponse(content=value, status_code=200)

    def append_event(conversation_id: str, event_type: str, payload: dict) -> None:
        conversation = state["conversations"][conversation_id]
        session = state["sessions"].get(conversation["session_id"], {})
        seq = conversation["run"]["last_seq"] + 1
        state["events"][conversation_id].append(
            {
                "schema_version": "1",
                "event_id": str(_uuid4()),
                "run_id": conversation["run"]["run_id"],
                "seq": seq,
                "type": event_type,
                "payload": payload,
                "tenant_id": session.get("tenant_id"),
                "user_id": session.get("user_id"),
                "project_id": session.get("project_id"),
                "source": "runner",
                "occurred_at": now,
            }
        )
        conversation["run"]["last_seq"] = seq

    @app.get("/live")
    async def live():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        return {
            "status": "ready",
            "execution_mode": "temporal",
            "persistence_mode": "postgres",
            "instance_id": instance,
        }

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics():
        return (
            "agentsupport_queue_ready 0\n"
            "agentsupport_active_runtimes 0\n"
            "agentsupport_claims_expired 0\n"
            "agentsupport_outbox_pending 0\n"
        )

    @app.get("/cores")
    async def cores():
        return [
            {
                "type": "session_runner",
                "version": "0.1.0",
                "capabilities": ["run", "input", "checkpoint", "cancel", "events"],
            }
        ]

    @app.post("/workspaces", status_code=201)
    async def create_workspace(request: Request):
        body = await request.json()
        return idempotent_created(
            "workspace",
            request.headers.get("Idempotency-Key"),
            body,
            lambda: _create_workspace(body),
        )

    def _create_workspace(body: dict) -> dict:
        item = {
            "id": str(_uuid4()),
            "name": body["name"],
            "root_path": f"/workspace/{_uuid4()}",
            "created_at": now,
        }
        state["workspaces"][item["id"]] = item
        return item

    @app.post("/sessions", status_code=201)
    async def create_session(request: Request):
        body = await request.json()
        if body.get("workspace_id") not in state["workspaces"]:
            return error("WORKSPACE_NOT_FOUND", "workspace not found", 404)
        return idempotent_created(
            "session",
            request.headers.get("Idempotency-Key"),
            body,
            lambda: _create_session(body),
        )

    def _create_session(body: dict) -> dict:
        item = {
            "id": str(_uuid4()),
            "workspace_id": body["workspace_id"],
            "tenant_id": body.get("tenant_id"),
            "user_id": body.get("user_id"),
            "project_id": body.get("project_id"),
            "metadata": body.get("metadata") or {},
            "config": body.get("config"),
            "lease_epoch": 0,
            "active_container_id": None,
            "active_run_id": None,
            "created_at": now,
        }
        state["sessions"][item["id"]] = item
        return item

    @app.get("/sessions/{session_id}")
    async def get_session(session_id: _UUID):
        session_id = str(session_id)
        if session_id not in state["sessions"]:
            return error("SESSION_NOT_FOUND", "session not found", 404)
        return state["sessions"][session_id]

    @app.get("/sessions")
    async def list_sessions(
        tenant_id: str | None = None,
        user_id: str | None = None,
        project_id: str | None = None,
    ):
        items = list(state["sessions"].values())
        if tenant_id is not None:
            items = [item for item in items if item.get("tenant_id") == tenant_id]
        if user_id is not None:
            items = [item for item in items if item.get("user_id") == user_id]
        if project_id is not None:
            items = [item for item in items if item.get("project_id") == project_id]
        return items

    @app.get("/sessions/{session_id}/conversations")
    async def list_session_conversations(session_id):
        session_id = str(session_id)
        return [
            item
            for item in state["conversations"].values()
            if item["session_id"] == session_id
        ]

    @app.get("/sessions/{session_id}/events")
    async def session_events(session_id, after_seq: int = Query(default=0, ge=0)):
        session_id = str(session_id)
        events = [
            event
            for conversation_id, items in state["events"].items()
            if state["conversations"][conversation_id]["session_id"] == session_id
            for event in items
            if event["seq"] > after_seq
        ]
        return sorted(events, key=lambda event: event["occurred_at"])


    @app.post("/sessions/{session_id}/conversations", status_code=201)
    async def create_conversation(session_id, request: Request):
        session_id = str(session_id)
        if session_id not in state["sessions"]:
            return error("SESSION_NOT_FOUND", "session not found", 404)
        body = await request.json()
        if not body.get("task"):
            return error("INVALID_TASK", "task cannot be empty", 422)
        return idempotent_created(
            "conversation",
            request.headers.get("Idempotency-Key"),
            body,
            lambda: _create_conversation(session_id, body),
        )

    def _create_conversation(session_id: str, body: dict) -> dict:
        conversation_id = str(_uuid4())
        run_id = str(_uuid4())
        item = {
            "id": conversation_id,
            "session_id": session_id,
            "parent_conversation_id": body.get("parent_conversation_id"),
            "task": body["task"],
            "created_at": now,
            "run": {
                "run_id": run_id,
                "state": "RUNNING",
                "last_seq": 0,
                "pending_interaction": None,
                "result_summary": None,
                "checkpoint_id": None,
            },
        }
        state["conversations"][conversation_id] = item
        state["events"][conversation_id] = []
        if body["task"].startswith("ask:"):
            item["run"]["state"] = "WAITING_INPUT"
            interaction = {
                "interaction_id": str(_uuid4()),
                "kind": "input",
                "question": body["task"][4:],
            }
            item["run"]["pending_interaction"] = interaction
            append_event(conversation_id, "interaction.requested", interaction)
        else:
            append_event(
                conversation_id,
                "message",
                {"content": f"completed: {body['task']}"},
            )
            append_event(
                conversation_id,
                "run.completed",
                {"result": {"status": "completed"}},
            )
            item["run"]["state"] = "COMPLETED"
        return item

    @app.get("/conversations/{conversation_id}")
    async def get_conversation(conversation_id):
        conversation_id = str(conversation_id)
        if conversation_id not in state["conversations"]:
            return error("CONVERSATION_NOT_FOUND", "conversation not found", 404)
        return state["conversations"][conversation_id]

    @app.get("/conversations/{conversation_id}/events")
    async def list_events(conversation_id, after_seq: int = Query(default=0, ge=0)):
        conversation_id = str(conversation_id)
        if conversation_id not in state["conversations"]:
            return error("CONVERSATION_NOT_FOUND", "conversation not found", 404)
        return [
            event
            for event in state["events"][conversation_id]
            if event["seq"] > after_seq
        ]

    @app.get("/conversations/{conversation_id}/events/stream")
    async def stream_events(
        conversation_id, after_seq: int = Query(default=0, ge=0)
    ):
        conversation_id = str(conversation_id)
        if conversation_id not in state["conversations"]:
            raise HTTPException(404, "conversation not found")
        events = [
            event
            for event in state["events"][conversation_id]
            if event["seq"] > after_seq
        ]

        def body():
            for event in events:
                yield f"id: {event['seq']}\ndata: {_json.dumps(event)}\n\n"

        return StreamingResponse(body(), media_type="text/event-stream")

    @app.post("/conversations/{conversation_id}/input")
    async def submit_input(conversation_id, request: Request):
        conversation_id = str(conversation_id)
        if conversation_id not in state["conversations"]:
            return error("CONVERSATION_NOT_FOUND", "conversation not found", 404)
        conversation = state["conversations"][conversation_id]
        body = await request.json()

        def apply():
            expected_seq = body.get("expected_seq")
            if (
                expected_seq is not None
                and expected_seq != conversation["run"]["last_seq"]
            ):
                return error(
                    "CONFLICT", "expected_seq does not match conversation", 409
                )
            pending = conversation["run"].get("pending_interaction")
            if not pending or conversation["run"]["state"] != "WAITING_INPUT":
                return error(
                    "INVALID_STATE", "conversation is not waiting for input", 409
                )
            if body.get("interaction_id") != pending["interaction_id"]:
                return error(
                    "INTERACTION_NOT_FOUND",
                    "interaction does not match pending interaction",
                    409,
                )
            append_event(
                conversation_id,
                "interaction.input",
                {
                    "interaction_id": body["interaction_id"],
                    "value": body.get("value"),
                },
            )
            append_event(
                conversation_id,
                "run.completed",
                {"result": {"status": "completed"}},
            )
            conversation["run"]["state"] = "COMPLETED"
            conversation["run"]["pending_interaction"] = None
            return conversation

        value, _created = idempotent(
            "input", request.headers.get("Idempotency-Key"), body, apply
        )
        if isinstance(value, JSONResponse):
            return value
        return value

    @app.post("/conversations/{conversation_id}/approval")
    async def submit_approval(conversation_id, request: Request):
        conversation_id = str(conversation_id)
        if conversation_id not in state["conversations"]:
            return error("CONVERSATION_NOT_FOUND", "conversation not found", 404)
        body = await request.json()
        decision = body.get("decision")
        conversation = state["conversations"][conversation_id]

        def apply():
            if decision not in {"APPROVE_ONCE", "REJECT"}:
                return error(
                    "INVALID_DECISION",
                    "decision must be APPROVE_ONCE or REJECT",
                    422,
                )
            expected_seq = body.get("expected_seq")
            if (
                expected_seq is not None
                and expected_seq != conversation["run"]["last_seq"]
            ):
                return error(
                    "CONFLICT", "expected_seq does not match conversation", 409
                )
            pending = conversation["run"].get("pending_interaction")
            if not pending or conversation["run"]["state"] != "WAITING_INPUT":
                return error(
                    "INVALID_STATE",
                    "conversation is not waiting for approval",
                    409,
                )
            if body.get("approval_id") != pending["interaction_id"]:
                return error(
                    "INTERACTION_NOT_FOUND",
                    "approval does not match pending interaction",
                    409,
                )
            append_event(
                conversation_id,
                "approval.decided",
                {"approval_id": body["approval_id"], "decision": decision},
            )
            append_event(
                conversation_id,
                "run.completed",
                {"result": {"status": "completed", "approval": decision}},
            )
            conversation["run"]["state"] = "COMPLETED"
            conversation["run"]["pending_interaction"] = None
            return conversation

        value, _created = idempotent(
            "approval", request.headers.get("Idempotency-Key"), body, apply
        )
        if isinstance(value, JSONResponse):
            return value
        return value

    @app.post("/conversations/{conversation_id}/cancel")
    async def cancel(conversation_id, request: Request):
        conversation_id = str(conversation_id)
        if conversation_id not in state["conversations"]:
            return error("CONVERSATION_NOT_FOUND", "conversation not found", 404)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - cancel allows an empty request body
            body = {}
        conversation = state["conversations"][conversation_id]

        def apply():
            expected_seq = body.get("expected_seq")
            if (
                expected_seq is not None
                and expected_seq != conversation["run"]["last_seq"]
            ):
                return error(
                    "CONFLICT", "expected_seq does not match conversation", 409
                )
            if conversation["run"]["state"] not in {
                "COMPLETED",
                "FAILED",
                "CANCELLED",
                "LOST",
            }:
                append_event(conversation_id, "run.cancelled", {})
                conversation["run"]["state"] = "CANCELLED"
                conversation["run"]["pending_interaction"] = None
            return conversation

        value, _created = idempotent(
            "cancel", request.headers.get("Idempotency-Key"), body, apply
        )
        if isinstance(value, JSONResponse):
            return value
        return value

    return app


@pytest.mark.asyncio
async def test_api_acceptance_operation_produces_structured_report(tmp_path):
    upstream = _fake_agentsupport_app()
    controller = DevConsoleController(
        command_runner=FakeCommandRunner(),
        agentsupport_url="http://agentsupport.test",
        agentsupport_transport=ASGITransport(app=upstream),
    )
    console = create_dev_console_app(
        controller,
        token="accept-token",
        monitor_log_dir=tmp_path,
    )
    async with AsyncClient(
        transport=ASGITransport(app=console), base_url="http://console"
    ) as client:
        response = await client.post(
            "/api/actions/api-accept",
            headers={"X-Dev-Console-Token": "accept-token"},
            json={
                "include_negative": True,
                "include_sse": True,
                "include_task_flow": True,
            },
        )
        operation = await _wait_for_operation(
            client, response.json()["id"], "accept-token"
        )

    assert response.status_code == 202
    assert operation["status"] == "succeeded", operation.get("error")
    result = operation["result"]
    assert result["summary"]["total"] == 14
    assert result["summary"]["failed"] == 0
    assert result["coverage"]["operations_covered"] == result["coverage"][
        "operations_total"
    ]
    assert result["coverage"]["uncovered"] == []
    check_ids = {check["id"] for check in result["checks"]}
    assert {
        "OP-01",
        "OP-02",
        "OP-03",
        "OP-04",
        "RS-01",
        "RS-02",
        "RS-03",
        "CV-01",
        "CV-02",
        "CV-03",
        "CV-04",
        "IN-01",
        "IN-02",
        "IN-03",
    } <= check_ids

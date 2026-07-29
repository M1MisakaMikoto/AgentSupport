import asyncio
import re
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient

from devtools.console.app import (
    AcceptanceRequest,
    CommandResult,
    DevConsoleController,
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
    for _ in range(50):
        response = await client.get(
            f"/api/operations/{operation_id}",
            headers={"X-Dev-Console-Token": token},
        )
        if response.json()["status"] in {"succeeded", "failed"}:
            return response.json()
        await asyncio.sleep(0.01)
    raise AssertionError("operation did not finish")


@pytest.mark.asyncio
async def test_dev_console_requires_token_and_runs_fixed_deploy_commands(monkeypatch):
    runner = FakeCommandRunner()
    controller = DevConsoleController(command_runner=runner)

    async def ready():
        return {"status": "ready"}

    monkeypatch.setattr(controller, "_wait_ready", ready)
    app = create_dev_console_app(controller, token="test-token")
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
    assert denied.status_code == 403
    assert accepted.status_code == 202
    assert operation["status"] == "succeeded"
    assert runner.calls[0]["command"][-2:] == ["compose", "build"]
    assert runner.calls[1]["command"][-4:] == [
        "--scale",
        "api=2",
        "--scale",
        "worker=3",
    ]
    assert runner.calls[1]["env"] == {"SESSION_RUNNER_MODE": "deterministic"}


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
async def test_acceptance_operation_uses_controlled_test_commands(monkeypatch):
    runner = FakeCommandRunner()
    controller = DevConsoleController(command_runner=runner)

    async def live_acceptance(request, operation):
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
    assert any(command[2:4] == ["ruff", "check"] for command in commands)
    assert sum("pytest" in command for command in commands) == 2
    postgres_call = next(
        call
        for call in runner.calls
        if any("test_postgres_distributed.py" in argument for argument in call["command"])
    )
    assert postgres_call["env"] == {
        "PYTHONUTF8": "1",
        "RUN_POSTGRES_DISTRIBUTED_TESTS": "1",
    }
    assert not any(re.search(r"[;&|]", argument) for command in commands for argument in command)


@pytest.mark.asyncio
async def test_one_stop_console_serves_task_ui_and_proxies_api_and_sse():
    platform = FastAPI()

    @platform.get("/live")
    async def live():
        return {"status": "ok"}

    @platform.post("/echo")
    async def echo(request: Request):
        return {
            "body": await request.json(),
            "idempotency_key": request.headers.get("Idempotency-Key"),
            "console_token": request.headers.get("X-Dev-Console-Token"),
        }

    @platform.get("/events")
    async def events():
        async def body():
            yield "id: 1\ndata: {\"type\":\"ready\"}\n\n"

        return StreamingResponse(body(), media_type="text/event-stream")

    controller = DevConsoleController(
        command_runner=FakeCommandRunner(),
        platform_url="http://platform.test",
        platform_transport=ASGITransport(app=platform),
    )
    app = create_dev_console_app(controller, token="proxy-token")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://console") as client:
        task_ui = await client.get("/tasks/")
        task_script = await client.get("/tasks/app.js")
        live_response = await client.get("/platform/live")
        echo_response = await client.post(
            "/platform/echo",
            headers={
                "Idempotency-Key": "proxy-once",
                "X-Dev-Console-Token": "must-not-forward",
            },
            json={"value": "forwarded"},
        )
        event_response = await client.get("/platform/events")

    assert task_ui.status_code == 200
    assert "验收控制台" in task_ui.text
    assert "window.location.origin}/platform" in task_script.text
    assert live_response.json() == {"status": "ok"}
    assert echo_response.json() == {
        "body": {"value": "forwarded"},
        "idempotency_key": "proxy-once",
        "console_token": None,
    }
    assert event_response.headers["content-type"].startswith("text/event-stream")
    assert 'data: {"type":"ready"}' in event_response.text


def test_windows_launcher_starts_loopback_console_without_starting_stack():
    root = Path(__file__).resolve().parents[3]
    powershell = (root / "start-console.ps1").read_text(encoding="utf-8")
    command = (root / "start-console.cmd").read_text(encoding="utf-8")

    assert "devtools.console" in powershell
    assert "http://127.0.0.1:8010/" in powershell
    assert "compose" not in powershell.lower()
    assert "start-console.ps1" in command

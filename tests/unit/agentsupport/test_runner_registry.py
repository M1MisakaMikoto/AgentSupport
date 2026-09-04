"""Unit tests for Runner registration, heartbeat and expiry in the service."""

from datetime import UTC, datetime, timedelta

import pytest

from agent_runner_contracts.registration import (
    RUNNER_CAPABILITIES,
    RunnerHeartbeat,
    RunnerRegistrationRequest,
)
from agentsupport.application.service import ServiceError
from _support import make_temporal_service as _make_service


def _service(tmp_path, *, token: str = "bootstrap-secret"):
    return _make_service(tmp_path, runner_token=token).service


def _request() -> RunnerRegistrationRequest:
    return RunnerRegistrationRequest(
        provider="deterministic",
        endpoint="http://127.0.0.1:8080",
        version="0.1.0",
        capabilities=list(RUNNER_CAPABILITIES),
    )


def test_registration_requires_configured_token(tmp_path):
    service = _service(tmp_path, token="")
    with pytest.raises(ServiceError) as exc:
        service.register_runner(_request(), bootstrap_token="anything")
    assert exc.value.code == "RUNNER_REGISTRATION_DISABLED"
    assert exc.value.status_code == 503


def test_registration_rejects_wrong_bootstrap_token(tmp_path):
    service = _service(tmp_path)
    with pytest.raises(ServiceError) as exc:
        service.register_runner(_request(), bootstrap_token="wrong")
    assert exc.value.code == "RUNNER_TOKEN_INVALID"
    assert exc.value.status_code == 401


def test_register_heartbeat_deregister_roundtrip(tmp_path):
    service = _service(tmp_path)
    response = service.register_runner(_request(), bootstrap_token="bootstrap-secret")
    assert response.runner_id is not None
    assert response.token

    snapshot = service.runner_snapshot()
    assert len(snapshot) == 1
    assert snapshot[0]["type"] == "deterministic"
    assert "run" in snapshot[0]["capabilities"]

    heartbeat = service.runner_heartbeat(
        response.runner_id,
        RunnerHeartbeat(status="READY", load=1),
        runner_token=response.token,
    )
    assert heartbeat.load == 1

    with pytest.raises(ServiceError) as exc:
        service.runner_heartbeat(
            response.runner_id,
            RunnerHeartbeat(),
            runner_token="not-the-runner-token",
        )
    assert exc.value.code == "RUNNER_TOKEN_INVALID"

    service.runner_deregister(response.runner_id, runner_token=response.token)
    assert service.runner_snapshot() == []
    with pytest.raises(ServiceError) as exc:
        service.runner_heartbeat(
            response.runner_id, RunnerHeartbeat(), runner_token=response.token
        )
    assert exc.value.code == "RUNNER_NOT_FOUND"


def test_duplicate_registration_conflicts(tmp_path):
    service = _service(tmp_path)
    service.register_runner(_request(), bootstrap_token="bootstrap-secret")
    with pytest.raises(ServiceError) as exc:
        service.register_runner(_request(), bootstrap_token="bootstrap-secret")
    assert exc.value.code == "RUNNER_ALREADY_REGISTERED"
    assert exc.value.status_code == 409


def test_stale_runners_are_pruned(tmp_path):
    service = _service(tmp_path)
    response = service.register_runner(_request(), bootstrap_token="bootstrap-secret")
    stale_at = datetime.now(UTC) - timedelta(seconds=service.config.runner_heartbeat_timeout_seconds + 5)
    service.runner_registry.repository.update_runner_registration(
        response.runner_id,
        status="READY",
        load=1,
        capabilities=list(RUNNER_CAPABILITIES),
        heartbeat_at=stale_at,
    )

    assert service.prune_stale_runners() == 1
    assert service.runner_snapshot() == []


async def test_registered_runner_is_used_for_temporal_routing(tmp_path):
    env = _make_service(tmp_path, runner_token="bootstrap-secret")
    service = env.service
    response = service.register_runner(_request(), bootstrap_token="bootstrap-secret")
    service.runner_heartbeat(
        response.runner_id,
        RunnerHeartbeat(status="READY", load=1),
        runner_token=response.token,
    )
    workspace = service.create_workspace("demo")
    session = service.create_session(workspace.id)
    conversation = await service.create_conversation(session.id, "task")
    await env.coordinator.wait_for_run(str(conversation.run.run_id), timeout_seconds=15)

    assert env.fake.captured[0]["runner_url"] == "http://127.0.0.1:8080"


def test_select_ready_runner_prefers_lowest_load(tmp_path):
    service = _service(tmp_path)
    first = service.register_runner(
        RunnerRegistrationRequest(
            provider="trae",
            endpoint="http://runner-a:8080",
            capabilities=list(RUNNER_CAPABILITIES),
        ),
        bootstrap_token="bootstrap-secret",
    )
    second = service.register_runner(
        RunnerRegistrationRequest(
            provider="trae",
            endpoint="http://runner-b:8080",
            capabilities=list(RUNNER_CAPABILITIES),
        ),
        bootstrap_token="bootstrap-secret",
    )
    service.runner_heartbeat(
        first.runner_id,
        RunnerHeartbeat(status="READY", load=5),
        runner_token=first.token,
    )
    service.runner_heartbeat(
        second.runner_id,
        RunnerHeartbeat(status="READY", load=0),
        runner_token=second.token,
    )

    selected = service.select_ready_runner(set(RUNNER_CAPABILITIES))
    assert selected is not None
    assert selected.runner_id == second.runner_id

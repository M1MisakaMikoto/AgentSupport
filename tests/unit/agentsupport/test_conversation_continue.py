"""Continue after a retryable failure: a fresh round in the same session."""

from __future__ import annotations

import pytest
from _support import make_temporal_service as _make_service
from httpx import ASGITransport, AsyncClient

from agentsupport.api import create_app
from agentsupport.application.service import ServiceError
from agentsupport.domain import Conversation, ExecutionState
from agentsupport.execution.temporal.activities import fail_run


def _env(tmp_path):
    return _make_service(
        tmp_path,
        workspace_root=tmp_path / "workspaces",
        skills_root=tmp_path / "skills",
    )


def _running_conversation(env, tmp_path, task: str = "算一下季度毛利"):
    repository = env.repository
    workspace = repository.create_workspace(
        "ws", str(tmp_path / "workspaces" / "ws"), "hash-ws", None
    )
    session = repository.create_session(workspace, "hash-session", None)
    conversation = Conversation(session_id=session.id, task=task)
    conversation.run.state = ExecutionState.RUNNING
    return session, repository.create_conversation(conversation, "hash-conv", None)


async def _fail(conversation: Conversation, *, retryable: bool) -> None:
    error: dict[str, object] = {"code": "TRAE_RUNTIME_ERROR", "message": "read timeout"}
    if retryable:
        error["retryable"] = True
    result = await fail_run(
        {
            "request": {
                "conversation_id": str(conversation.id),
                "run_id": str(conversation.run.run_id),
            },
            "error": error,
        }
    )
    assert result["status"] == "failed"


async def test_continue_creates_a_fresh_round_in_the_same_session(tmp_path):
    env = _env(tmp_path)
    session, conversation = _running_conversation(env, tmp_path)
    await _fail(conversation, retryable=True)

    continued = await env.service.continue_conversation(conversation.id)

    assert continued.session_id == session.id
    assert continued.task == conversation.task
    assert continued.parent_conversation_id == conversation.id
    assert continued.id != conversation.id


async def test_continue_rejects_a_run_that_did_not_fail(tmp_path):
    env = _env(tmp_path)
    _, conversation = _running_conversation(env, tmp_path)

    with pytest.raises(ServiceError) as exc:
        await env.service.continue_conversation(conversation.id)

    assert exc.value.code == "CONVERSATION_NOT_FAILED"
    assert exc.value.status_code == 409


async def test_continue_rejects_a_non_retryable_failure(tmp_path):
    env = _env(tmp_path)
    _, conversation = _running_conversation(env, tmp_path)
    await _fail(conversation, retryable=False)

    with pytest.raises(ServiceError) as exc:
        await env.service.continue_conversation(conversation.id)

    assert exc.value.code == "CONVERSATION_NOT_RETRYABLE"
    assert exc.value.status_code == 409


async def test_continue_endpoint_returns_the_new_conversation(tmp_path):
    env = _env(tmp_path)
    _, conversation = _running_conversation(env, tmp_path)
    await _fail(conversation, retryable=True)
    app = create_app(env.service)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/conversations/{conversation.id}/continue")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["session_id"] == str(conversation.session_id)
    assert body["parent_conversation_id"] == str(conversation.id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        rejected = await client.post(f"/conversations/{body['id']}/continue")

    assert rejected.status_code in {409, 404}

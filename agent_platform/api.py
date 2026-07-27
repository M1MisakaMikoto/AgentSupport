from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .services import PlatformService, ServiceError


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class SessionCreate(BaseModel):
    workspace_id: UUID


class ConversationCreate(BaseModel):
    task: str = Field(min_length=1)
    parent_conversation_id: UUID | None = None


class InteractionRequest(BaseModel):
    interaction_id: str
    value: Any
    expected_seq: int | None = None


class ApprovalRequest(BaseModel):
    approval_id: str
    decision: str
    expected_seq: int | None = None


class CancelRequest(BaseModel):
    expected_seq: int | None = None


def create_app(service: PlatformService | None = None) -> FastAPI:
    selected_service = service or PlatformService()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def pause_worker() -> None:
            while True:
                await asyncio.sleep(selected_service.config.pause_worker_interval_seconds)
                await selected_service.pause_expired_waiting()

        async def health_worker() -> None:
            while True:
                await asyncio.sleep(selected_service.config.health_check_interval_seconds)
                await selected_service.supervise_active_sessions()

        worker = asyncio.create_task(pause_worker())
        health = asyncio.create_task(health_worker())
        try:
            yield
        finally:
            worker.cancel()
            health.cancel()
            with suppress(asyncio.CancelledError):
                await worker
            with suppress(asyncio.CancelledError):
                await health

    app = FastAPI(title="Agent Platform", version="0.1.0", lifespan=lifespan)
    app.state.service = selected_service

    @app.middleware("http")
    async def correlation_id(request: Request, call_next):
        request.state.correlation_id = request.headers.get("X-Correlation-ID") or str(uuid4())
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = request.state.correlation_id
        return response

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.code,
                "message": exc.message,
                "retryable": exc.status_code >= 500 or exc.status_code == 429,
                "operation": f"{request.method} {request.url.path}",
                "correlation_id": request.state.correlation_id,
                "details": exc.details,
            },
        )

    @app.get("/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/workspaces", status_code=201)
    async def create_workspace(
        body: WorkspaceCreate, idempotency_key: str | None = Header(default=None)
    ):
        return app.state.service.create_workspace(body.name, idempotency_key)

    @app.post("/sessions", status_code=201)
    async def create_session(
        body: SessionCreate, idempotency_key: str | None = Header(default=None)
    ):
        return app.state.service.create_session(body.workspace_id, idempotency_key)

    @app.post("/sessions/{session_id}/conversations", status_code=201)
    async def create_conversation(
        session_id: UUID,
        body: ConversationCreate,
        idempotency_key: str | None = Header(default=None),
    ):
        return await app.state.service.create_conversation(
            session_id, body.task, body.parent_conversation_id, idempotency_key
        )

    @app.get("/conversations/{conversation_id}/events")
    async def list_events(conversation_id: UUID, after_seq: int = Query(default=0, ge=0)):
        return app.state.service.events(conversation_id, after_seq)

    @app.get("/conversations/{conversation_id}/events/stream")
    async def stream_events(conversation_id: UUID, after_seq: int = Query(default=0, ge=0)):
        app.state.service._conversation(conversation_id)

        async def body():
            async for event in app.state.service.events_store.stream(conversation_id, after_seq):
                yield f"id: {event.seq}\ndata: {json.dumps(event.model_dump(mode='json'))}\n\n"

        return StreamingResponse(body(), media_type="text/event-stream")

    @app.get("/sessions/{session_id}/events")
    async def session_events(session_id: UUID, after_seq: int = Query(default=0, ge=0)):
        if session_id not in app.state.service.sessions:
            raise ServiceError("SESSION_NOT_FOUND", "session does not exist", 404)
        conversations = [
            conversation
            for conversation in app.state.service.conversations.values()
            if conversation.session_id == session_id
        ]
        events = [
            event
            for conversation in conversations
            for event in app.state.service.events_store.list(conversation.id, after_seq)
        ]
        return sorted(events, key=lambda event: event.occurred_at)

    @app.post("/conversations/{conversation_id}/input")
    async def submit_input(
        conversation_id: UUID,
        body: InteractionRequest,
        idempotency_key: str | None = Header(default=None),
    ):
        return await app.state.service.submit_input(
            conversation_id,
            body.interaction_id,
            body.value,
            body.expected_seq,
            idempotency_key,
        )

    @app.post("/conversations/{conversation_id}/approval")
    async def submit_approval(
        conversation_id: UUID,
        body: ApprovalRequest,
        idempotency_key: str | None = Header(default=None),
    ):
        return await app.state.service.submit_approval(
            conversation_id,
            body.approval_id,
            body.decision,
            body.expected_seq,
            idempotency_key,
        )

    @app.post("/conversations/{conversation_id}/cancel")
    async def cancel(
        conversation_id: UUID,
        body: CancelRequest | None = None,
        idempotency_key: str | None = Header(default=None),
    ):
        return await app.state.service.cancel(
            conversation_id,
            body.expected_seq if body else None,
            idempotency_key,
        )

    @app.get("/cores")
    async def list_cores():
        return [
            {
                "type": "session_runner",
                "version": "0.1.0",
                "capabilities": ["run", "input", "checkpoint", "cancel", "events"],
            }
        ]

    app.mount(
        "/debug",
        StaticFiles(directory=Path(__file__).parent / "debug_ui", html=True),
        name="debug-ui",
    )

    return app


app = create_app()

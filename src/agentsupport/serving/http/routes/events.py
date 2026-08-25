import asyncio
import json
import os
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from ..dependencies import agentsupport_service

router = APIRouter()


def _keepalive_seconds() -> int:
    try:
        return max(1, int(os.getenv("AGENTSUPPORT_SSE_KEEPALIVE_SECONDS", "15")))
    except ValueError:
        return 15


def _resume_seq(after_seq: int | None, last_event_id: str | None) -> int:
    """Resolve the SSE resume cursor.

    An explicit ``after_seq`` query parameter wins; otherwise the standard
    ``Last-Event-ID`` reconnection header is used; otherwise start from 0.
    """

    if after_seq is not None:
        return after_seq
    if last_event_id and last_event_id.isdigit():
        return int(last_event_id)
    return 0


@router.get("/conversations/{conversation_id}/events")
def list_events(
    request: Request,
    conversation_id: UUID,
    after_seq: int = Query(default=0, ge=0),
    limit: int | None = Query(default=None, ge=1, le=1000),
):
    return agentsupport_service(request).events(conversation_id, after_seq, limit)


@router.get("/conversations/{conversation_id}/events/stream")
async def stream_events(
    request: Request,
    conversation_id: UUID,
    after_seq: int | None = Query(default=None, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    service = agentsupport_service(request)
    service._conversation(conversation_id)
    resume_after = _resume_seq(after_seq, last_event_id)

    async def body():
        events = service.stream_events(conversation_id, resume_after)
        keepalive = _keepalive_seconds()
        next_event_task = None
        while True:
            if next_event_task is None or next_event_task.done():
                next_event_task = asyncio.create_task(anext(events))
            done, _pending = await asyncio.wait(
                {next_event_task, asyncio.create_task(asyncio.sleep(keepalive))},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if next_event_task not in done:
                # Idle streams must keep the connection alive for proxies with
                # read timeouts; a comment frame is ignored by SSE clients.
                yield ": keepalive\n\n"
                continue
            try:
                event = next_event_task.result()
            except StopAsyncIteration:
                break
            next_event_task = None
            yield f"id: {event.seq}\ndata: {json.dumps(event.model_dump(mode='json'))}\n\n"

    return StreamingResponse(body(), media_type="text/event-stream")


@router.get("/sessions/{session_id}/events")
def session_events(
    request: Request,
    session_id: UUID,
    after_seq: int = Query(default=0, ge=0),
    limit: int | None = Query(default=None, ge=1, le=1000),
):
    return agentsupport_service(request).session_events(session_id, after_seq, limit)


@router.get("/sessions/{session_id}/events/stream")
async def stream_session_events(
    request: Request,
    session_id: UUID,
    after_seq: int | None = Query(default=None, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    service = agentsupport_service(request)
    service.get_session(session_id)
    resume_after = _resume_seq(after_seq, last_event_id)

    async def body():
        events = service.stream_session_events(session_id, resume_after)
        keepalive = _keepalive_seconds()
        next_event_task = None
        while True:
            if next_event_task is None or next_event_task.done():
                next_event_task = asyncio.create_task(anext(events))
            done, _pending = await asyncio.wait(
                {next_event_task, asyncio.create_task(asyncio.sleep(keepalive))},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if next_event_task not in done:
                yield ": keepalive\n\n"
                continue
            try:
                event = next_event_task.result()
            except StopAsyncIteration:
                break
            next_event_task = None
            yield f"id: {event.seq}\ndata: {json.dumps(event.model_dump(mode='json'))}\n\n"

    return StreamingResponse(body(), media_type="text/event-stream")

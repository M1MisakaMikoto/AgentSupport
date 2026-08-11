import json
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from ..dependencies import agentsupport_service

router = APIRouter()


@router.get("/conversations/{conversation_id}/events")
async def list_events(
    request: Request, conversation_id: UUID, after_seq: int = Query(default=0, ge=0)
):
    return agentsupport_service(request).events(conversation_id, after_seq)


@router.get("/conversations/{conversation_id}/events/stream")
async def stream_events(
    request: Request, conversation_id: UUID, after_seq: int = Query(default=0, ge=0)
):
    service = agentsupport_service(request)
    service._conversation(conversation_id)

    async def body():
        async for event in service.stream_events(conversation_id, after_seq):
            yield f"id: {event.seq}\ndata: {json.dumps(event.model_dump(mode='json'))}\n\n"

    return StreamingResponse(body(), media_type="text/event-stream")


@router.get("/sessions/{session_id}/events")
async def session_events(
    request: Request, session_id: UUID, after_seq: int = Query(default=0, ge=0)
):
    return agentsupport_service(request).session_events(session_id, after_seq)


@router.get("/sessions/{session_id}/events/stream")
async def stream_session_events(
    request: Request, session_id: UUID, after_seq: int = Query(default=0, ge=0)
):
    service = agentsupport_service(request)
    service.get_session(session_id)

    async def body():
        async for event in service.stream_session_events(session_id, after_seq):
            yield f"id: {event.seq}\ndata: {json.dumps(event.model_dump(mode='json'))}\n\n"

    return StreamingResponse(body(), media_type="text/event-stream")

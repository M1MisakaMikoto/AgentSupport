from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from ...application.service import AgentSupportService
from ...bootstrap.container import build_agentsupport_service
from .errors import install_error_handlers
from .routes import events_router, interactions_router, operations_router, resources_router


def create_app(service: AgentSupportService | None = None) -> FastAPI:
    selected_service = service or build_agentsupport_service()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if selected_service.distributed:
            yield
            return

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

    app = FastAPI(title="AgentSupport", version="0.1.0", lifespan=lifespan)
    app.state.service = selected_service

    @app.middleware("http")
    async def correlation_id(request: Request, call_next):
        request.state.correlation_id = request.headers.get("X-Correlation-ID") or str(uuid4())
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = request.state.correlation_id
        response.headers["X-AgentSupport-Instance"] = app.state.service.instance_id
        return response

    install_error_handlers(app)
    app.include_router(operations_router)
    app.include_router(resources_router)
    app.include_router(events_router)
    app.include_router(interactions_router)
    app.mount(
        "/debug",
        StaticFiles(directory=Path(__file__).parent / "static" / "debug", html=True),
        name="debug-ui",
    )
    return app


app = create_app()

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from ...application.service import AgentSupportService
from ...bootstrap.container import build_agentsupport_service, build_eval_service
from ...observability import context as obs_context
from ...observability import logging as obs_logging
from ...observability import metrics as obs_metrics
from ...observability import tracing as obs_tracing
from .api_docs import api_reference_description
from .errors import install_error_handlers
from .routes import (
    diagnostics_router,
    eval_router,
    events_router,
    interactions_router,
    mcp_servers_router,
    operations_router,
    registrations_router,
    resources_router,
    skills_router,
)


def create_app(
    service: AgentSupportService | None = None,
    *,
    eval_service=None,
) -> FastAPI:
    selected_service = service or build_agentsupport_service()
    selected_eval_service = eval_service or build_eval_service(
        selected_service.config, agentsupport=selected_service
    )
    obs_logging.configure_logging(
        log_format=selected_service.config.log_format,
        level=selected_service.config.log_level,
        service_name=selected_service.config.service_name,
    )
    obs_tracing.init_tracing(
        service_name=selected_service.config.service_name,
        endpoint=selected_service.config.otel_exporter_otlp_endpoint,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if selected_service.temporal_mode:
            yield
            return

        async def pause_worker() -> None:
            while True:
                await asyncio.sleep(selected_service.config.pause_worker_interval_seconds)
                selected_service.prune_retained_state()
                selected_service.prune_stale_runners()

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

    app = FastAPI(
        title="AgentSupport",
        version="0.2.0",
        description=api_reference_description(),
        lifespan=lifespan,
    )
    app.state.service = selected_service
    app.state.eval_service = selected_eval_service
    obs_tracing.instrument_app(app)

    @app.middleware("http")
    async def observability_middleware(request: Request, call_next):
        request.state.correlation_id = request.headers.get("X-Correlation-ID") or str(uuid4())
        obs_context.set_correlation_id(request.state.correlation_id)
        route = request.scope.get("route")
        route_path = getattr(route, "path", None) or request.url.path
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Correlation-ID"] = request.state.correlation_id
            response.headers["X-AgentSupport-Instance"] = app.state.service.instance_id
            return response
        finally:
            obs_metrics.record_http(
                request.method, route_path, status, time.perf_counter() - started
            )
            obs_tracing.sync_trace_ids()

    install_error_handlers(app)
    app.include_router(operations_router)
    app.include_router(resources_router)
    app.include_router(diagnostics_router)
    app.include_router(events_router)
    app.include_router(eval_router)
    app.include_router(interactions_router)
    app.include_router(mcp_servers_router)
    app.include_router(registrations_router)
    app.include_router(skills_router)
    app.mount(
        "/debug",
        StaticFiles(directory=Path(__file__).parent / "static" / "debug", html=True),
        name="debug-ui",
    )
    return app


app = create_app()

"""HTTP surface and background loop of the host-side runner manager."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from .manager import RunnerManager
from .settings import ManagerSettings

logger = logging.getLogger(__name__)


def create_manager_app(
    settings: ManagerSettings | None = None,
    *,
    manager: RunnerManager | None = None,
    build_loop: bool = True,
) -> FastAPI:
    selected = settings or ManagerSettings()
    core = manager or RunnerManager(selected)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task: asyncio.Task[None] | None = None
        if build_loop:
            task = asyncio.create_task(_build_loop(core))
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="AgentSupport Runner Manager", version="0.1.0", lifespan=lifespan)
    app.state.manager = core

    @app.middleware("http")
    async def _authorize(request: Request, call_next):
        if selected.token and request.url.path != "/health":
            provided = request.headers.get("X-Runner-Manager-Token") or ""
            if not secrets.compare_digest(provided, selected.token):
                raise HTTPException(401, "invalid runner manager token")
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "platform_url": selected.platform_url,
            "base_image": selected.base_image,
            "runners": len(core.runners()),
        }

    @app.get("/runners")
    async def list_runners() -> list[dict[str, Any]]:
        return core.runners()

    @app.post("/ensure-runner")
    async def ensure_runner(body: dict[str, Any]) -> dict[str, Any]:
        tenant_id = str(body.get("tenant_id") or "default")
        image_tag = str(body.get("image_tag") or selected.base_image)
        try:
            return await core.ensure_runner(tenant_id, image_tag)
        except Exception as exc:
            logger.warning("ensure-runner failed for %s: %s", tenant_id, exc)
            raise HTTPException(502, f"cannot start runner: {exc}") from exc

    @app.post("/runners/{tenant_id}/stop")
    async def stop_runner(tenant_id: str) -> dict[str, Any]:
        return {"stopped": await core.stop_runner(tenant_id)}

    return app


async def _build_loop(core: RunnerManager) -> None:
    """Poll the platform for preset builds and reclaim idle runners."""

    while True:
        worked = False
        try:
            worked = await core.build_once()
            await core.reap_idle()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must survive outages
            logger.warning("runner manager loop error: %s", exc)
        if not worked:
            await asyncio.sleep(core.settings.poll_interval_seconds)


app = create_manager_app()

__all__ = ["app", "create_manager_app"]

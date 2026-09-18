"""Runner self-registration client for the private Session Runner."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import httpx

from agent_runner_contracts.registration import (
    RUNNER_CAPABILITIES,
    RunnerHeartbeat,
    RunnerRegistrationRequest,
    RunnerRegistrationResponse,
)

logger = logging.getLogger(__name__)


class RunnerRegistrationClient:
    """Registers this Runner with the control plane and keeps it healthy.

    Registration failures are non-fatal: the Runner keeps serving and the
    control plane falls back to static runner URLs where configured.
    """

    def __init__(
        self,
        *,
        control_plane_url: str,
        bootstrap_token: str,
        endpoint: str,
        provider: str,
        version: str = "0.1.0",
        capabilities: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        heartbeat_seconds: float = 10.0,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.control_plane_url = control_plane_url.rstrip("/")
        self.bootstrap_token = bootstrap_token
        self.endpoint = endpoint.rstrip("/")
        self.provider = provider
        self.version = version
        self.capabilities = list(capabilities or RUNNER_CAPABILITIES)
        self.metadata = dict(metadata or {})
        self.heartbeat_seconds = heartbeat_seconds
        self.timeout = timeout_seconds
        self.transport = transport
        self._response: RunnerRegistrationResponse | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.control_plane_url,
            timeout=self.timeout,
            transport=self.transport,
        )

    def registered(self) -> bool:
        return self._response is not None

    async def register(self) -> bool:
        payload = RunnerRegistrationRequest(
            provider=self.provider,
            endpoint=self.endpoint,
            version=self.version,
            capabilities=self.capabilities,
            metadata=self.metadata,
        )
        try:
            async with self._client() as client:
                response = await client.post(
                    "/runners/register",
                    json=payload.model_dump(mode="json"),
                    headers={"X-Runner-Token": self.bootstrap_token},
                )
                response.raise_for_status()
                self._response = RunnerRegistrationResponse.model_validate(response.json())
        except Exception as exc:  # noqa: BLE001 - registration must never crash the runner
            logger.warning("runner registration failed: %s", exc)
            return False
        logger.info(
            "registered runner %s provider=%s endpoint=%s",
            self._response.runner_id,
            self.provider,
            self.endpoint,
        )
        return True

    async def _heartbeat_once(self) -> bool:
        if self._response is None:
            return False
        payload = RunnerHeartbeat(status="READY", load=0)
        try:
            async with self._client() as client:
                response = await client.post(
                    f"/runners/{self._response.runner_id}/heartbeat",
                    json=payload.model_dump(mode="json"),
                    headers={"X-Runner-Token": self._response.token},
                )
                response.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("runner heartbeat failed: %s", exc)
            return False

    async def heartbeat_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.heartbeat_seconds
                )
                return
            except TimeoutError:
                await self._heartbeat_once()

    async def deregister(self) -> bool:
        if self._response is None:
            return False
        try:
            async with self._client() as client:
                response = await client.delete(
                    f"/runners/{self._response.runner_id}",
                    headers={"X-Runner-Token": self._response.token},
                )
                response.raise_for_status()
            logger.info("deregistered runner %s", self._response.runner_id)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("runner deregistration failed: %s", exc)
            return False

    def start(self) -> None:
        """Start the heartbeat background task (call from the app lifespan)."""

        if self._task is None or self._task.done():
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self.heartbeat_forever())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.deregister()


def runner_registration_client_from_env(
    *,
    mode: str,
    endpoint: str | None = None,
    heartbeat_seconds: float | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RunnerRegistrationClient | None:
    """Build a client from environment variables, or None when not configured."""

    control_plane_url = _env("AGENTSUPPORT_CONTROL_PLANE_URL")
    bootstrap_token = _env("SESSION_RUNNER_TOKEN")
    if not control_plane_url or not bootstrap_token:
        return None
    return RunnerRegistrationClient(
        control_plane_url=control_plane_url,
        bootstrap_token=bootstrap_token,
        endpoint=endpoint or _env("SESSION_RUNNER_ENDPOINT") or "http://127.0.0.1:8080",
        provider=_env("SESSION_RUNNER_PROVIDER") or mode,
        version=_env("SESSION_RUNNER_VERSION") or "0.1.0",
        metadata={"tenant_id": _optional_tenant_id()},
        heartbeat_seconds=heartbeat_seconds
        or _float_env("SESSION_RUNNER_HEARTBEAT_SECONDS", 10.0),
        transport=transport,
    )


def _env(name: str) -> str | None:
    import os

    value = os.getenv(name)
    return value.strip() if value else None


def _optional_tenant_id() -> str:
    """The tenant this runner was launched for (empty for a shared runner)."""

    return _env("SESSION_RUNNER_TENANT_ID") or ""


def _float_env(name: str, default: float) -> float:
    import os

    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


__all__ = ["RunnerRegistrationClient", "runner_registration_client_from_env"]

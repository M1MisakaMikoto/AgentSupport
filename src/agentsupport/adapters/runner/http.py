from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx

from agent_runner_contracts.checkpoint import Checkpoint
from agent_runner_contracts.events import EventEnvelope

from ...application.ports import EventSink

TERMINAL_RUN_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "LOST"})


class TraeCoreRunnerRuntime:
    """HTTP/JSON CoreRuntime adapter for a private Session Runner."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds
        self.transport = transport
        self._run_urls: dict[UUID, str] = {}
        self._shared_client: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        """One reusable client (connection pool) for the process lifetime.

        Creating a fresh ``AsyncClient`` per call means a fresh connection pool
        (and usually a fresh TCP connection) for every health check, input,
        cancel etc. The health supervisor polls every few seconds per active
        session, so reuse removes steady connection churn.
        """

        if self._shared_client is None:
            self._shared_client = httpx.AsyncClient(
                timeout=self.timeout,
                transport=self.transport,
            )
        return self._shared_client

    async def aclose(self) -> None:
        if self._shared_client is not None:
            await self._shared_client.aclose()
            self._shared_client = None

    def register_run_endpoint(self, run_id: UUID, base_url: str | None) -> None:
        if base_url:
            self._run_urls[run_id] = base_url.rstrip("/")

    def unregister_run_endpoint(self, run_id: UUID) -> None:
        self._run_urls.pop(run_id, None)

    def _run_url(self, run_id: UUID) -> str:
        return self._run_urls.get(run_id, self.base_url)

    def _url(self, run_id: UUID | None, path: str) -> str:
        base = self._run_url(run_id) if run_id is not None else self.base_url
        return f"{base}{path}"

    async def health(self, run_id: UUID | None = None) -> dict[str, Any]:
        client = self._client()
        live = await client.get(self._url(run_id, "/live"))
        live.raise_for_status()
        ready = await client.get(self._url(run_id, "/ready"))
        ready.raise_for_status()
        return {"live": live.json(), "ready": ready.json()}

    async def model_connectivity(self) -> dict[str, Any]:
        response = await self._client().get(
            self._url(None, "/diagnostics/model-connectivity")
        )
        response.raise_for_status()
        return response.json()

    async def run(self, request: dict[str, Any], event_sink: EventSink) -> dict[str, Any]:
        run_id = UUID(str(request["run_id"]))
        runner_url = request.get("runner_url")
        self.register_run_endpoint(run_id, runner_url)
        response = await self._client().post(self._url(run_id, "/runs"), json=request)
        response.raise_for_status()
        result = response.json()
        for event in result.get("events", []):
            await event_sink(EventEnvelope.model_validate(event))
        if result.get("status") in TERMINAL_RUN_STATUSES:
            self.unregister_run_endpoint(run_id)
        return result

    async def accept_input(
        self,
        run_id: UUID,
        interaction_id: str,
        value: Any,
        *,
        command_id: UUID | None = None,
    ) -> dict[str, Any]:
        response = await self._client().post(
            self._url(run_id, f"/runs/{run_id}/input"),
            json={
                "interaction_id": interaction_id,
                "value": value,
                "command_id": str(command_id) if command_id else None,
            },
        )
        response.raise_for_status()
        result = response.json()
        if result.get("status") in TERMINAL_RUN_STATUSES:
            self.unregister_run_endpoint(run_id)
        return result

    async def accept_approval(
        self,
        run_id: UUID,
        approval_id: str,
        decision: str,
        *,
        command_id: UUID | None = None,
    ) -> dict[str, Any]:
        response = await self._client().post(
            self._url(run_id, f"/runs/{run_id}/approval"),
            json={
                "approval_id": approval_id,
                "decision": decision,
                "command_id": str(command_id) if command_id else None,
            },
        )
        response.raise_for_status()
        result = response.json()
        if result.get("status") in TERMINAL_RUN_STATUSES:
            self.unregister_run_endpoint(run_id)
        return result

    async def checkpoint(self, run_id: UUID, reason: str) -> Checkpoint:
        response = await self._client().post(
            self._url(run_id, f"/runs/{run_id}/checkpoint"), json={"reason": reason}
        )
        response.raise_for_status()
        return Checkpoint.model_validate(response.json())

    async def resume(
        self,
        checkpoint: Checkpoint,
        value: Any,
        event_sink: EventSink,
        *,
        command_id: UUID | None = None,
        runtime_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "checkpoint": checkpoint.model_dump(mode="json"),
            "value": value,
            "command_id": str(command_id) if command_id else None,
            **(runtime_context or {}),
        }
        response = await self._client().post(
            self._url(checkpoint.run_id, f"/runs/{checkpoint.run_id}/resume"),
            json=payload,
        )
        response.raise_for_status()
        result = response.json()
        for event in result.get("events", []):
            await event_sink(EventEnvelope.model_validate(event))
        if result.get("status") in TERMINAL_RUN_STATUSES:
            self.unregister_run_endpoint(checkpoint.run_id)
        return result

    async def cancel(
        self, run_id: UUID, *, command_id: UUID | None = None
    ) -> dict[str, Any]:
        response = await self._client().post(
            self._url(run_id, f"/runs/{run_id}/cancel"),
            json={"command_id": str(command_id) if command_id else None},
        )
        response.raise_for_status()
        result = response.json()
        if result.get("status") in {"COMPLETED", "FAILED", "CANCELLED", "LOST"}:
            self.unregister_run_endpoint(run_id)
        return result

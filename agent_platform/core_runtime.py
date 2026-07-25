from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx

from .domain import Checkpoint
from .events import EventEnvelope
from .ports import EventSink


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

    def _client(self, base_url: str | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=(base_url or self.base_url).rstrip("/"),
            timeout=self.timeout,
            transport=self.transport,
        )

    def register_run_endpoint(self, run_id: UUID, base_url: str | None) -> None:
        if base_url:
            self._run_urls[run_id] = base_url.rstrip("/")

    def unregister_run_endpoint(self, run_id: UUID) -> None:
        self._run_urls.pop(run_id, None)

    def _run_url(self, run_id: UUID) -> str:
        return self._run_urls.get(run_id, self.base_url)

    async def health(self, run_id: UUID | None = None) -> dict[str, Any]:
        async with self._client(self._run_url(run_id) if run_id else None) as client:
            live = await client.get("/live")
            live.raise_for_status()
            ready = await client.get("/ready")
            ready.raise_for_status()
            return {"live": live.json(), "ready": ready.json()}

    async def run(self, request: dict[str, Any], event_sink: EventSink) -> dict[str, Any]:
        run_id = UUID(str(request["run_id"]))
        runner_url = request.get("runner_url")
        self.register_run_endpoint(run_id, runner_url)
        async with self._client(self._run_url(run_id)) as client:
            response = await client.post("/runs", json=request)
            response.raise_for_status()
            result = response.json()
        for event in result.get("events", []):
            await event_sink(EventEnvelope.model_validate(event))
        return result

    async def accept_input(self, run_id: UUID, interaction_id: str, value: Any) -> dict[str, Any]:
        async with self._client(self._run_url(run_id)) as client:
            response = await client.post(
                f"/runs/{run_id}/input",
                json={"interaction_id": interaction_id, "value": value},
            )
            response.raise_for_status()
            return response.json()

    async def accept_approval(
        self, run_id: UUID, approval_id: str, decision: str
    ) -> dict[str, Any]:
        async with self._client(self._run_url(run_id)) as client:
            response = await client.post(
                f"/runs/{run_id}/approval",
                json={"approval_id": approval_id, "decision": decision},
            )
            response.raise_for_status()
            return response.json()

    async def checkpoint(self, run_id: UUID, reason: str) -> Checkpoint:
        async with self._client(self._run_url(run_id)) as client:
            response = await client.post(f"/runs/{run_id}/checkpoint", json={"reason": reason})
            response.raise_for_status()
            return Checkpoint.model_validate(response.json())

    async def resume(
        self, checkpoint: Checkpoint, value: Any, event_sink: EventSink
    ) -> dict[str, Any]:
        async with self._client(self._run_url(checkpoint.run_id)) as client:
            response = await client.post(
                f"/runs/{checkpoint.run_id}/resume",
                json={"checkpoint": checkpoint.model_dump(mode="json"), "value": value},
            )
            response.raise_for_status()
            result = response.json()
        for event in result.get("events", []):
            await event_sink(EventEnvelope.model_validate(event))
        return result

    async def cancel(self, run_id: UUID) -> dict[str, Any]:
        async with self._client(self._run_url(run_id)) as client:
            response = await client.post(f"/runs/{run_id}/cancel")
            response.raise_for_status()
            result = response.json()
        if result.get("status") in {"COMPLETED", "FAILED", "CANCELLED", "LOST"}:
            self.unregister_run_endpoint(run_id)
        return result

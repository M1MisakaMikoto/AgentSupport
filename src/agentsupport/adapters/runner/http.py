from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any
from uuid import UUID

import httpx

from agent_runner_contracts.checkpoint import Checkpoint
from agent_runner_contracts.events import EventEnvelope

from ...application.ports import EventSink

logger = logging.getLogger(__name__)

TERMINAL_RUN_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "LOST"})
READY_PROBE_TIMEOUT_SECONDS = 5.0
#: POST /runs 返回后，给订阅协程多少时间把剩余事件搬完（正常是毫秒级）。
RUN_EVENT_DRAIN_SECONDS = 5.0
#: 事件订阅未建立（run 还没注册上 → 404）或断线后的续订间隔。
RUN_EVENT_SUBSCRIBE_RETRY_SECONDS = 0.2


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
        await self._require_ready(run_id)
        # ⚠️ `POST /runs` 要等 run **暂停或结束**才返回，返回体里才带事件。于是"中途没有暂停点"
        # 的 run（大多数）事件会**全部堆到最后一刻**才进平台/DB —— 前端整轮看不到任何进展
        # （2026-09-17 实测：2,154 条事件全在 run 结束时入库，调用方多等 36s）。
        # 这里让 POST 在后台跑，同时**订阅 runner 的事件流**把事件到一条 sink 一条。
        # 落库要求 seq 严格连续（`append_event`: last_seq+1），所以订阅端只按 seq 递增地补。
        post_task = asyncio.ensure_future(
            self._client().post(self._url(run_id, "/runs"), json=request)
        )
        progress = [0]                          # 订阅端已落库到哪条 seq（超时/断开时也可见）
        stopped = asyncio.Event()               # POST 返回并收尾后置位 → 订阅协程停手
        subscription = asyncio.ensure_future(
            self._stream_run_events(run_id, event_sink, progress, stopped)
        )
        response = await post_task
        response.raise_for_status()
        result = response.json()
        # POST 返回即 run 已终态：runner 的订阅会自己收尾。等它把剩余事件搬完，
        # 再补 POST 返回体里"订阅没来得及搬"的尾巴 —— 两者都按 seq 递增，不重复落库。
        try:
            await asyncio.wait_for(subscription, timeout=RUN_EVENT_DRAIN_SECONDS)
        except asyncio.CancelledError:
            stopped.set()
            subscription.cancel()
            raise
        except asyncio.TimeoutError:
            logger.warning("run %s 的事件订阅 %ss 内没收尾，取消后按 POST 返回体补齐",
                           run_id, RUN_EVENT_DRAIN_SECONDS)
            stopped.set()
            subscription.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await subscription
        except Exception:  # noqa: BLE001 - 订阅彻底失败不算 run 失败，返回体里仍有完整事件
            logger.warning("run %s 的事件订阅中断，改用 POST 返回体补齐", run_id, exc_info=True)
        else:
            stopped.set()
        seen = progress[0]
        for event in result.get("events", []):
            seq = int(event.get("seq") or 0)
            if seq > seen:                      # 已经订阅 sink 过的不要重复落库（会撞 seq 冲突）
                await event_sink(EventEnvelope.model_validate(event))
                seen = seq
        if result.get("status") in TERMINAL_RUN_STATUSES:
            self.unregister_run_endpoint(run_id)
        return result

    async def _stream_run_events(
        self,
        run_id: UUID,
        event_sink: EventSink,
        progress: list[int],
        stopped: asyncio.Event,
    ) -> None:
        """订阅 `GET /runs/{run_id}/events/stream`（SSE），把事件按 seq 顺序 sink 给平台。

        `progress[0]` 记录已落库的 seq（调用方在超时/取消后靠它避免重复落库）。
        断线/订阅过早（run 还没注册上 → 404）都按 `progress[0]` 续订；
        订阅彻底失败**不影响**整轮 run：`POST /runs` 的返回体里仍有完整事件列表，
        `run()` 会用 `seq > progress[0]` 补齐（并打告警）。
        """

        url = self._url(run_id, f"/runs/{run_id}/events/stream")
        while not stopped.is_set():
            try:
                async with self._client().stream(
                    "GET",
                    url,
                    params={"after_seq": progress[0]},
                    timeout=httpx.Timeout(self.timeout, read=None, pool=None),
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if stopped.is_set():
                            return
                        if not line.startswith("data: "):
                            continue
                        envelope = EventEnvelope.model_validate(json.loads(line[6:]))
                        if envelope.seq <= progress[0]:
                            continue
                        await event_sink(envelope)
                        progress[0] = envelope.seq
                return              # 流正常收尾 = run 已终态且事件已推完
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001  404/断线：稍后续订，不静默丢事件
                logger.info("run %s 的事件订阅未建立/中断（after_seq=%s），%.1fs 后续订：%s",
                            run_id, progress[0], RUN_EVENT_SUBSCRIBE_RETRY_SECONDS, exc)
                await asyncio.sleep(RUN_EVENT_SUBSCRIBE_RETRY_SECONDS)

    async def _require_ready(self, run_id: UUID) -> None:
        """Fail fast when the runner is wedged instead of queueing forever.

        A frozen runner still accepts TCP connections, so without this probe a
        new run would sit in the socket backlog with no events emitted at all.
        """

        try:
            response = await self._client().get(
                self._url(run_id, "/ready"), timeout=READY_PROBE_TIMEOUT_SECONDS
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"session runner is not ready: {exc}") from exc

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

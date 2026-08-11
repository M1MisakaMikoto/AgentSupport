"""Structured HTTP contract acceptance for the AgentSupport public API.

The verifier drives the live AgentSupport HTTP API through its public surface
only (no direct service calls) and produces a structured, repeatable report
that the development console renders.  Every check records the OpenAPI
operation pattern it exercises so the report can also show operation coverage
against the runtime ``/openapi.json``.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx

REQUIRED_METRICS = {
    "agentsupport_queue_ready",
    "agentsupport_active_runtimes",
    "agentsupport_claims_expired",
    "agentsupport_outbox_pending",
}
REQUIRED_CORE_CAPABILITIES = {"run", "input", "checkpoint", "cancel", "events"}
EXPECTED_EVENT_FIELDS = {
    "schema_version",
    "event_id",
    "run_id",
    "seq",
    "type",
    "payload",
    "tenant_id",
    "user_id",
    "project_id",
    "source",
    "occurred_at",
}

CheckWork = Callable[[], Awaitable[str]]


@dataclass(slots=True)
class ApiCheck:
    id: str
    title: str
    method: str
    operation: str
    operations: list[str] = field(default_factory=list)
    status: str = "pending"
    detail: str = ""
    duration_ms: int = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "method": self.method,
            "operation": self.operation,
            "operations": self.operations,
            "status": self.status,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
        }


class ApiContractVerifier:
    """Runs the public API contract battery against a live AgentSupport API."""

    def __init__(
        self,
        *,
        base_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30,
        suffix: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.timeout = timeout
        self.suffix = suffix or uuid4().hex[:10]
        self.checks: list[ApiCheck] = []
        self._client: httpx.AsyncClient | None = None
        self._ctx: dict[str, Any] = {}

    @property
    def client(self) -> httpx.AsyncClient:
        assert self._client is not None
        return self._client

    def _key(self, scope: str) -> str:
        return f"api-accept-{scope}-{self.suffix}"

    async def _record(
        self,
        check_id: str,
        title: str,
        method: str,
        operation: str,
        work: CheckWork,
        *,
        operations: list[str] | None = None,
        emit: Callable[[str, str], None] | None = None,
    ) -> None:
        started = time.monotonic()
        check = ApiCheck(
            id=check_id,
            title=title,
            method=method,
            operation=operation,
            operations=operations or [f"{method.upper()} {operation}"],
        )
        self.checks.append(check)
        try:
            detail = await work()
        except AssertionError as exc:
            check.status = "fail"
            check.detail = str(exc)
        except httpx.HTTPStatusError as exc:
            check.status = "fail"
            check.detail = (
                f"unexpected HTTP {exc.response.status_code}: "
                f"{exc.response.text[:400]}"
            )
        except Exception as exc:  # noqa: BLE001 - check failures are report rows
            check.status = "fail"
            check.detail = f"{type(exc).__name__}: {exc}"
        else:
            check.status = "pass"
            check.detail = detail or ""
        check.duration_ms = int((time.monotonic() - started) * 1000)
        if emit:
            emit(
                "result",
                f"{check.status.upper()} {check.id} {check.operation} - "
                f"{check.detail or 'ok'}",
            )

    async def _wait_for_events(
        self,
        conversation_id: str,
        predicate: Callable[[list[dict[str, Any]]], bool],
        *,
        timeout: float,
    ) -> list[dict[str, Any]]:
        deadline = asyncio.get_running_loop().time() + timeout
        events: list[dict[str, Any]] = []
        while asyncio.get_running_loop().time() < deadline:
            response = await self.client.get(
                f"/conversations/{conversation_id}/events?after_seq=0"
            )
            response.raise_for_status()
            events = response.json()
            if predicate(events):
                return events
            await asyncio.sleep(0.3)
        return events

    async def _collect_sse(
        self,
        url: str,
        *,
        stop_types: set[str],
        timeout: float,
    ) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        deadline = asyncio.get_running_loop().time() + timeout
        async with self.client.stream("GET", url) as response:
            response.raise_for_status()
            current: dict[str, Any] = {}
            async for line in response.aiter_lines():
                if asyncio.get_running_loop().time() >= deadline:
                    break
                if not line.strip():
                    if current:
                        try:
                            current["data"] = json.loads(current.get("data", "{}"))
                        except json.JSONDecodeError:
                            pass
                        collected.append(current)
                        if stop_types and current.get("data", {}).get("type") in stop_types:
                            break
                        current = {}
                    continue
                if line.startswith("id: "):
                    current["id"] = line[4:].strip()
                elif line.startswith("data: "):
                    current["data"] = line[6:]
        return collected

    # ------------------------------------------------------------------ ops
    async def _check_live(self) -> str:
        response = await self.client.get("/live")
        assert response.status_code == 200, f"expected 200, got {response.status_code}"
        assert response.json() == {"status": "ok"}, response.text
        assert response.headers.get("x-correlation-id"), "missing X-Correlation-ID"
        assert response.headers.get(
            "x-agentsupport-instance"
        ), "missing X-AgentSupport-Instance"
        return "200 ok, correlation/instance headers present"

    async def _check_ready(self) -> str:
        response = await self.client.get("/ready")
        assert response.status_code == 200, f"expected 200, got {response.status_code}"
        body = response.json()
        assert body.get("status") == "ready", response.text
        missing = {
            "execution_mode",
            "persistence_mode",
            "instance_id",
        } - set(body)
        assert not missing, f"missing ready fields: {sorted(missing)}"
        return (
            f"ready ({body.get('execution_mode')}/{body.get('persistence_mode')})"
        )

    async def _check_metrics(self) -> str:
        response = await self.client.get("/metrics")
        assert response.status_code == 200, f"expected 200, got {response.status_code}"
        assert response.headers.get("content-type", "").startswith(
            "text/plain"
        ), response.headers.get("content-type")
        names = {
            line.split()[0]
            for line in response.text.splitlines()
            if line.strip()
        }
        missing = sorted(REQUIRED_METRICS - names)
        assert not missing, f"missing metrics: {missing}"
        return f"{len(REQUIRED_METRICS)} required metrics present"

    async def _check_cores(self) -> str:
        response = await self.client.get("/cores")
        assert response.status_code == 200, f"expected 200, got {response.status_code}"
        cores = response.json()
        assert isinstance(cores, list), "expected a core list"
        if not cores:
            return "no registered runners (empty directory)"
        for item in cores:
            assert item.get("type"), "core type missing"
            assert item.get("version"), "core version missing"
            missing = REQUIRED_CORE_CAPABILITIES - set(item.get("capabilities", []))
            assert not missing, f"missing core capabilities: {sorted(missing)}"
        return f"{len(cores)} registered runner(s) with full capability set"

    async def _scenario_operations(
        self, emit: Callable[[str, str], None]
    ) -> None:
        await self._record(
            "OP-01", "/live 存活与响应头", "GET", "/live", self._check_live, emit=emit
        )
        await self._record(
            "OP-02", "/ready 就绪与模式", "GET", "/ready", self._check_ready, emit=emit
        )
        await self._record(
            "OP-03",
            "/metrics 必需指标",
            "GET",
            "/metrics",
            self._check_metrics,
            emit=emit,
        )
        await self._record(
            "OP-04", "/cores 核心发现", "GET", "/cores", self._check_cores, emit=emit
        )

    # ------------------------------------------------------------ resources
    async def _check_workspace(self) -> str:
        name = f"api-accept-workspace-{self.suffix}"
        key = self._key("workspace")
        headers = {"Idempotency-Key": key}
        response = await self.client.post(
            "/workspaces", json={"name": name}, headers=headers
        )
        assert response.status_code == 201, (
            f"expected 201, got {response.status_code}: {response.text}"
        )
        body = response.json()
        for key in ("id", "name", "root_path", "created_at"):
            assert body.get(key), f"missing workspace field: {key}"
        assert body["name"] == name
        replay = await self.client.post(
            "/workspaces", json={"name": name}, headers=headers
        )
        assert replay.status_code == 200, f"replay expected 200, got {replay.status_code}"
        assert replay.json()["id"] == body["id"], "replay returned a different resource"
        conflict = await self.client.post(
            "/workspaces", json={"name": f"{name}-2"}, headers=headers
        )
        assert conflict.status_code == 409, (
            f"key conflict expected 409, got {conflict.status_code}"
        )
        assert conflict.json().get("code") == "IDEMPOTENCY_CONFLICT"
        self._ctx["workspace_id"] = body["id"]
        return f"201 {body['id']}, replay idempotent, conflict 409"

    async def _check_session(self, include_negative: bool) -> str:
        response = await self.client.post(
            "/sessions",
            json={"workspace_id": self._ctx["workspace_id"]},
            headers={"Idempotency-Key": self._key("session")},
        )
        assert response.status_code == 201, (
            f"expected 201, got {response.status_code}: {response.text}"
        )
        body = response.json()
        for key in ("id", "workspace_id"):
            assert body.get(key), f"missing session field: {key}"
        self._ctx["session_id"] = body["id"]
        notes = [f"201 {body['id']}"]
        if include_negative:
            missing = await self.client.post(
                "/sessions", json={"workspace_id": str(uuid4())}
            )
            if missing.status_code == 201:
                assert missing.json().get("auto_created", {}).get("workspace", {}).get(
                    "id"
                ), "auto-created workspace id missing"
                notes.append("201 auto-created workspace, 422 invalid UUID")
            else:
                assert missing.status_code == 404, (
                    f"missing workspace expected 404, got {missing.status_code}"
                )
                assert missing.json().get("code") == "WORKSPACE_NOT_FOUND"
                notes.append("404 WORKSPACE_NOT_FOUND, 422 invalid UUID")
            invalid = await self.client.get("/sessions/not-a-uuid")
            assert invalid.status_code == 422, (
                f"invalid UUID expected 422, got {invalid.status_code}"
            )
        return ", ".join(notes)

    async def _check_labels_and_config(self) -> str:
        suffix = self.suffix
        labeled = await self.client.post(
            "/sessions",
            json={
                "workspace_id": self._ctx["workspace_id"],
                "tenant_id": f"tenant-{suffix}",
                "user_id": f"user-{suffix}",
                "project_id": f"project-{suffix}",
                "metadata": {"team": "platform"},
                "config": {
                    "skills": [{"skill_id": "review", "enabled": True}],
                    "tool_policy": {
                        "allowed_tools": ["bash", "task_done"],
                        "approval_required_tools": ["bash"],
                    },
                },
            },
            headers={"Idempotency-Key": self._key("labeled-session")},
        )
        assert labeled.status_code == 201, labeled.text
        session_id = labeled.json()["id"]
        assert labeled.json()["tenant_id"] == f"tenant-{suffix}", labeled.text
        assert labeled.json()["user_id"] == f"user-{suffix}", labeled.text
        assert labeled.json()["project_id"] == f"project-{suffix}", labeled.text
        assert labeled.json().get("metadata") == {"team": "platform"}, labeled.text

        filtered = await self.client.get(
            "/sessions", params={"tenant_id": f"tenant-{suffix}"}
        )
        assert any(item["id"] == session_id for item in filtered.json()), filtered.text

        for path in ("/organizations", "/users", "/presets", "/projects"):
            gone = await self.client.get(path)
            assert gone.status_code == 404, (
                f"{path} expected 404, got {gone.status_code}"
            )

        conversation = await self.client.post(
            f"/sessions/{session_id}/conversations",
            json={"task": f"label-smoke-{suffix}"},
        )
        assert conversation.status_code == 201, conversation.text
        events = await self.client.get(
            f"/conversations/{conversation.json()['id']}/events"
        )
        assert events.status_code == 200
        assert all(
            event.get("tenant_id") == f"tenant-{suffix}" for event in events.json()
        ), events.text

        self._ctx["labeled_session_id"] = session_id
        return (
            "labels/config stored, tenant filter works, "
            "events carry labels, business routes 404"
        )

    async def _scenario_resources(
        self,
        emit: Callable[[str, str], None],
        *,
        include_negative: bool,
    ) -> None:
        await self._record(
            "RS-01",
            "Workspace 创建、幂等重放与 Key 冲突",
            "POST",
            "/workspaces",
            self._check_workspace,
            emit=emit,
        )
        await self._record(
            "RS-02",
            "Session 创建与错误路径",
            "POST",
            "/sessions",
            lambda: self._check_session(include_negative),
            operations=["POST /sessions", "GET /sessions/{session_id}"],
            emit=emit,
        )
        await self._record(
            "RS-03",
            "标签/配置透传与业务路由下线",
            "POST",
            "/sessions",
            self._check_labels_and_config,
            operations=[
                "POST /sessions",
                "GET /sessions",
                "GET /sessions/{session_id}",
                "POST /sessions/{session_id}/conversations",
                "GET /conversations/{conversation_id}/events",
            ],
            emit=emit,
        )

    # ------------------------------------------------------- conversations
    async def _check_conversation(self) -> str:
        session_id = self._ctx["session_id"]
        task = f"api-acceptance-{self.suffix}: complete"
        response = await self.client.post(
            f"/sessions/{session_id}/conversations",
            json={"task": task},
            headers={"Idempotency-Key": self._key("conversation")},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body.get("id"), "conversation id missing"
        assert body["task"] == task
        run = body.get("run") or {}
        assert run.get("run_id"), "run projection missing"
        self._ctx["conversation_id"] = body["id"]
        return f"201 {body['id']}, run {run['run_id']}"

    async def _check_conversation_queries(self) -> str:
        conversation_id = self._ctx["conversation_id"]
        session_id = self._ctx["session_id"]
        fetched = await self.client.get(f"/conversations/{conversation_id}")
        assert fetched.status_code == 200
        assert fetched.json()["id"] == conversation_id
        session_listing = await self.client.get(
            f"/sessions/{session_id}/conversations"
        )
        assert any(
            item["id"] == conversation_id for item in session_listing.json()
        )
        session_events = await self.client.get(f"/sessions/{session_id}/events")
        assert session_events.status_code == 200
        assert isinstance(session_events.json(), list)
        return "conversation/session queries ok"

    async def _check_events_cursor(self, include_negative: bool) -> str:
        conversation_id = self._ctx["conversation_id"]
        events = await self._wait_for_events(
            conversation_id,
            lambda items: any(item["type"] == "run.completed" for item in items),
            timeout=self.timeout * 2,
        )
        assert events, "no events received"
        assert any(
            item["type"] == "run.completed" for item in events
        ), f"run did not complete: {[e['type'] for e in events]}"
        first = events[0]
        missing = EXPECTED_EVENT_FIELDS - set(first)
        assert not missing, f"event missing fields: {sorted(missing)}"
        seqs = [item["seq"] for item in events]
        assert seqs == sorted(seqs), "event seq order is not ascending"
        assert len(seqs) == len(set(seqs)), "event seq not unique"
        last = seqs[-1]
        tail = await self.client.get(
            f"/conversations/{conversation_id}/events?after_seq={last}"
        )
        assert tail.status_code == 200
        assert all(
            item["seq"] > last for item in tail.json()
        ), "after_seq is not exclusive"
        assert not tail.json(), "terminal conversation produced events past last seq"
        notes = [
            f"{len(events)} events, ascending unique seq, cursor exclusive",
            f"tail after_seq={last} empty",
        ]
        if include_negative:
            missing_conv = await self.client.get(
                f"/conversations/{uuid4()}/events"
            )
            assert missing_conv.status_code == 404
            negative = await self.client.get(
                f"/conversations/{conversation_id}/events?after_seq=-1"
            )
            assert negative.status_code == 422, (
                f"negative cursor expected 422, got {negative.status_code}"
            )
            notes.append("404 unknown conversation, 422 negative cursor")
        return ", ".join(notes)

    async def _check_sse(self) -> str:
        conversation_id = self._ctx["conversation_id"]
        streamed = await self._collect_sse(
            f"/conversations/{conversation_id}/events/stream?after_seq=0",
            stop_types={"run.completed"},
            timeout=self.timeout * 2,
        )
        assert streamed, "SSE stream closed without events"
        ids = [int(item["id"]) for item in streamed]
        assert ids == sorted(ids), f"SSE ids not monotonic: {ids}"
        for item in streamed:
            assert int(item["id"]) == item["data"]["seq"], (
                "SSE id does not match event seq"
            )
        last = streamed[-1]["data"]["seq"]
        seen_ids = {item["data"]["event_id"] for item in streamed}
        extra = await self._collect_sse(
            f"/conversations/{conversation_id}/events/stream?after_seq={last}",
            stop_types=set(),
            timeout=3,
        )
        duplicates = [
            item["data"]["event_id"]
            for item in extra
            if item["data"]["event_id"] in seen_ids
        ]
        assert not duplicates, f"duplicate events after reconnect: {duplicates}"
        return f"{len(streamed)} SSE events, id==seq, reconnect clean"

    async def _scenario_conversations(
        self,
        emit: Callable[[str, str], None],
        *,
        include_negative: bool,
        include_sse: bool,
    ) -> None:
        await self._record(
            "CV-01",
            "Conversation 创建与 Run 投影",
            "POST",
            "/sessions/{session_id}/conversations",
            self._check_conversation,
            emit=emit,
        )
        await self._record(
            "CV-02",
            "Conversation/Session 查询与 Session 事件",
            "GET",
            "/conversations/{conversation_id}",
            self._check_conversation_queries,
            operations=[
                "GET /conversations/{conversation_id}",
                "GET /sessions/{session_id}/conversations",
                "GET /sessions/{session_id}/events",
            ],
            emit=emit,
        )
        await self._record(
            "CV-03",
            "事件列表、结构与游标",
            "GET",
            "/conversations/{conversation_id}/events",
            lambda: self._check_events_cursor(include_negative),
            emit=emit,
        )
        if include_sse:
            await self._record(
                "CV-04",
                "SSE 订阅、断线续传与去重",
                "GET",
                "/conversations/{conversation_id}/events/stream",
                self._check_sse,
                emit=emit,
            )

    # --------------------------------------------------------- interactions
    async def _ask_conversation(self, label: str) -> tuple[str, dict[str, Any]]:
        response = await self.client.post(
            f"/sessions/{self._ctx['session_id']}/conversations",
            json={"task": f"ask: Answer the acceptance question for {label}."},
            headers={"Idempotency-Key": self._key(f"ask-{label}")},
        )
        assert response.status_code == 201, response.text
        conversation_id = response.json()["id"]
        events = await self._wait_for_events(
            conversation_id,
            lambda items: any(
                item["type"] == "interaction.requested" for item in items
            ),
            timeout=self.timeout * 2,
        )
        interaction = next(
            item["payload"]
            for item in events
            if item["type"] == "interaction.requested"
        )
        last_seq = max(item["seq"] for item in events)
        return conversation_id, {"interaction": interaction, "last_seq": last_seq}

    async def _check_input_flow(self, include_negative: bool) -> str:
        conversation_id, pending = await self._ask_conversation("input")
        interaction = pending["interaction"]
        interaction_id = interaction["interaction_id"]
        last_seq = pending["last_seq"]
        notes: list[str] = []
        if include_negative:
            stale = await self.client.post(
                f"/conversations/{conversation_id}/input",
                json={
                    "interaction_id": interaction_id,
                    "value": "x",
                    "expected_seq": last_seq + 1,
                },
            )
            assert stale.status_code == 409, (
                f"stale expected_seq expected 409, got {stale.status_code}"
            )
            assert stale.json().get("code") == "CONFLICT"
            wrong = await self.client.post(
                f"/conversations/{conversation_id}/input",
                json={
                    "interaction_id": "definitely-wrong",
                    "value": "x",
                    "expected_seq": last_seq,
                },
            )
            assert wrong.status_code == 409, (
                f"wrong interaction id expected 409, got {wrong.status_code}"
            )
            assert wrong.json().get("code") == "INTERACTION_NOT_FOUND"
            notes.append("409 stale seq, 409 wrong interaction")

        key = self._key("input")
        submitted = await self.client.post(
            f"/conversations/{conversation_id}/input",
            json={
                "interaction_id": interaction_id,
                "value": {"answer": "42", "question": "universe"},
                "expected_seq": last_seq,
            },
            headers={"Idempotency-Key": key},
        )
        assert submitted.status_code == 200, submitted.text
        replay = await self.client.post(
            f"/conversations/{conversation_id}/input",
            json={
                "interaction_id": interaction_id,
                "value": {"answer": "42", "question": "universe"},
                "expected_seq": last_seq,
            },
            headers={"Idempotency-Key": key},
        )
        assert replay.status_code == 200
        assert replay.json()["id"] == conversation_id
        events = await self._wait_for_events(
            conversation_id,
            lambda items: any(item["type"] == "run.completed" for item in items),
            timeout=self.timeout * 2,
        )
        assert any(
            item["type"] == "run.completed" for item in events
        ), "input flow did not complete"
        notes.append("input resumed and completed, replay idempotent")
        return ", ".join(notes)

    async def _check_cancel_flow(self, include_negative: bool) -> str:
        conversation_id, pending = await self._ask_conversation("cancel")
        last_seq = pending["last_seq"]
        key = self._key("cancel")
        cancelled = await self.client.post(
            f"/conversations/{conversation_id}/cancel",
            json={"expected_seq": last_seq},
            headers={"Idempotency-Key": key},
        )
        assert cancelled.status_code == 200, cancelled.text
        events = await self._wait_for_events(
            conversation_id,
            lambda items: any(item["type"] == "run.cancelled" for item in items),
            timeout=self.timeout * 2,
        )
        assert any(
            item["type"] == "run.cancelled" for item in events
        ), "cancel did not produce run.cancelled"
        replay = await self.client.post(
            f"/conversations/{conversation_id}/cancel",
            json={"expected_seq": last_seq},
            headers={"Idempotency-Key": key},
        )
        assert replay.status_code == 200
        notes = ["WAITING_INPUT cancelled, run.cancelled emitted"]
        if include_negative:
            missing = await self.client.post(
                f"/conversations/{uuid4()}/cancel", json={}
            )
            assert missing.status_code == 404
            notes.append("404 unknown conversation")
        return ", ".join(notes)

    async def _check_approval_validation(self) -> str:
        conversation_id, pending = await self._ask_conversation("approval")
        interaction_id = pending["interaction"]["interaction_id"]
        last_seq = pending["last_seq"]
        invalid = await self.client.post(
            f"/conversations/{conversation_id}/approval",
            json={
                "approval_id": interaction_id,
                "decision": "MAYBE",
                "expected_seq": last_seq,
            },
        )
        assert invalid.status_code == 422, (
            f"invalid decision expected 422, got {invalid.status_code}"
        )
        assert invalid.json().get("code") == "INVALID_DECISION"
        return (
            "422 INVALID_DECISION validated; APPROVE_ONCE positive flow requires "
            "a tool_batch conversation (public API version boundary 0.1.0)"
        )

    async def _scenario_interactions(
        self,
        emit: Callable[[str, str], None],
        *,
        include_negative: bool,
    ) -> None:
        await self._record(
            "IN-01",
            "input 交互闭环与幂等",
            "POST",
            "/conversations/{conversation_id}/input",
            lambda: self._check_input_flow(include_negative),
            emit=emit,
        )
        await self._record(
            "IN-02",
            "approval 决策校验与版本边界",
            "POST",
            "/conversations/{conversation_id}/approval",
            self._check_approval_validation,
            emit=emit,
        )
        await self._record(
            "IN-03",
            "cancel 取消闭环与幂等",
            "POST",
            "/conversations/{conversation_id}/cancel",
            lambda: self._check_cancel_flow(include_negative),
            emit=emit,
        )

    # ------------------------------------------------------------ coverage
    async def _coverage(self) -> dict[str, Any]:
        try:
            response = await self.client.get("/openapi.json")
            response.raise_for_status()
            spec = response.json()
        except Exception as exc:  # noqa: BLE001 - coverage is a report section
            return {
                "error": f"{type(exc).__name__}: {exc}",
                "operations_total": 0,
                "operations_covered": 0,
                "uncovered": [],
            }
        operations: set[str] = set()
        for path, item in spec.get("paths", {}).items():
            for method in ("get", "post", "put", "patch", "delete"):
                if method in item:
                    operations.add(f"{method.upper()} {path}")
        covered: set[str] = set()
        for check in self.checks:
            covered.update(check.operations)
        uncovered = sorted(operations - covered)
        total = len(operations)
        matched = len(operations & covered)
        return {
            "operations_total": total,
            "operations_covered": matched,
            "coverage_pct": round(matched / total * 100, 1) if total else 100.0,
            "uncovered": uncovered,
        }

    async def run(
        self,
        *,
        include_negative: bool = True,
        include_sse: bool = True,
        include_task_flow: bool = True,
        emit: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        emit = emit or (lambda _stream, _message: None)
        emit("result", f"API contract acceptance started (run {self.suffix})")
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            transport=self.transport,
        ) as client:
            self._client = client
            await self._scenario_operations(emit)
            await self._scenario_resources(emit, include_negative=include_negative)
            if include_task_flow:
                await self._scenario_conversations(
                    emit,
                    include_negative=include_negative,
                    include_sse=include_sse,
                )
                await self._scenario_interactions(
                    emit, include_negative=include_negative
                )
            coverage = await self._coverage()

        counts = {"pass": 0, "fail": 0, "skip": 0}
        for check in self.checks:
            counts[check.status if check.status in counts else "fail"] += 1
        summary = {
            "total": len(self.checks),
            "passed": counts["pass"],
            "failed": counts["fail"],
            "skipped": counts["skip"],
            "success": counts["fail"] == 0 and bool(self.checks),
            "duration_ms": sum(check.duration_ms for check in self.checks),
            "suffix": self.suffix,
        }
        emit(
            "result",
            f"API contract acceptance finished: {summary['passed']}/{summary['total']} "
            f"passed, {summary['failed']} failed, coverage "
            f"{coverage.get('operations_covered', 0)}/"
            f"{coverage.get('operations_total', 0)}",
        )
        return {
            "summary": summary,
            "coverage": coverage,
            "checks": [check.snapshot() for check in self.checks],
            "resources": {
                key: value
                for key, value in self._ctx.items()
                if isinstance(value, str)
            },
        }

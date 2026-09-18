"""Temporal activities: durable run steps executed by the Temporal worker.

Each activity drives the real ``CoreRuntime`` (the session runner HTTP
adapter) and persists conversation events/state through the repository.
ExecutionJob rows, claims and RunCommand rows are not used in this mode; the
workflow event history takes over those responsibilities.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx
from temporalio import activity

from agent_runner_contracts.events import EventEnvelope

from ...adapters.persistence.sqlalchemy.repository import PostgresRepository
from ...adapters.skills.local import LocalSkillProvider
from ...application.ports import CoreRuntime
from ...application.workspace_refs import build_workspace_ref
from ...bootstrap.container import (
    build_core_runtime,
    build_repository,
    build_skill_provider,
)
from ...bootstrap.settings import Settings, settings
from ...domain import (
    TERMINAL_STATES,
    BuildStatus,
    Conversation,
    ConversationMode,
    ExecutionState,
)

ACTIVITY_HEARTBEAT_INTERVAL_SECONDS = 10.0

logger = logging.getLogger(__name__)


def _runner_tenant(entry: Any) -> str:
    return str((entry.metadata or {}).get("tenant_id") or "")


def _runner_matches_tenant(entry: Any, tenant_id: str | None) -> bool:
    """A tenant-specific runner only serves its own tenant."""

    return _runner_tenant(entry) == (tenant_id or "")


def _tenant_has_image(ctx: ExecutionContext, tenant_id: str | None) -> bool:
    """Whether this tenant has a READY preset image of its own."""

    if ctx.repository is None or not tenant_id or tenant_id == "default":
        return False
    return bool(
        ctx.repository.list_preset_builds(
            tenant_id=tenant_id, status=BuildStatus.READY, limit=1
        )
    )


def _registered_runner_url(ctx: ExecutionContext, tenant_id: str | None) -> str | None:
    """The least-loaded ready runner of this tenant, if one is registered."""

    if ctx.repository is None:
        return None
    ready = [
        entry
        for entry in ctx.repository.list_ready_runner_registrations()
        if "run" in (entry.capabilities or [])
    ]
    matched = [entry for entry in ready if _runner_matches_tenant(entry, tenant_id)]
    if matched:
        return min(matched, key=lambda entry: entry.load).endpoint
    if not _tenant_has_image(ctx, tenant_id):
        # No tenant image (yet): a shared runner is the right host for this
        # session, which keeps pre-tenant deployments working unchanged.
        shared = [entry for entry in ready if not _runner_tenant(entry) or _runner_tenant(entry) == "default"]
        if shared:
            return min(shared, key=lambda entry: entry.load).endpoint
    return None


def _static_runner_url(ctx: ExecutionContext) -> str:
    return (ctx.config.core_runner_url if ctx.config else None) or "http://runner"


def _select_runner_url(ctx: ExecutionContext, tenant_id: str | None = None) -> str:
    """Pick the least-loaded ready runner of this tenant, else the static URL."""

    return _registered_runner_url(ctx, tenant_id) or _static_runner_url(ctx)


async def _runner_url_for(ctx: ExecutionContext, tenant_id: str | None) -> str:
    """Registered tenant runner, else a freshly ensured one, else the static URL."""

    registered = _registered_runner_url(ctx, tenant_id)
    if registered:
        return registered
    ensured = await _ensure_runner_url(ctx, tenant_id)
    if ensured:
        return ensured
    return _static_runner_url(ctx)


async def _ensure_runner_url(ctx: ExecutionContext, tenant_id: str | None) -> str | None:
    """Ask the host-side runner manager for a ready runner of this tenant."""

    manager_url = str(getattr(ctx.config, "runner_manager_url", "") or "").strip()
    if not manager_url:
        return None
    image_tag = None
    if ctx.repository is not None and tenant_id:
        builds = ctx.repository.list_preset_builds(
            tenant_id=tenant_id, status=BuildStatus.READY, limit=1
        )
        if builds and builds[0].image_tag:
            image_tag = builds[0].image_tag
    if image_tag is None:
        image_tag = str(
            getattr(ctx.config, "default_runner_image", "agentsupport-api")
        )
    token = str(getattr(ctx.config, "runner_manager_token", "") or "")
    headers = {"X-Runner-Manager-Token": token} if token else {}
    try:
        async with httpx.AsyncClient(
            timeout=float(getattr(ctx.config, "runner_manager_timeout_seconds", 120.0))
        ) as client:
            response = await client.post(
                f"{manager_url.rstrip('/')}/ensure-runner",
                json={"tenant_id": tenant_id or "default", "image_tag": image_tag},
                headers=headers,
            )
            response.raise_for_status()
            body = response.json()
    except Exception as exc:  # noqa: BLE001 - manager outages fall back to the static URL
        logger.warning("runner manager ensure-runner failed: %s", exc)
        return None
    endpoint = body.get("endpoint")
    return str(endpoint) if endpoint else None


async def _heartbeat_until(stop: asyncio.Event, details: dict[str, Any]) -> None:
    """Keep the Temporal activity alive and cancellation-responsive.

    A long segment without heartbeats cannot be cancelled promptly and a dead
    worker is only detected after ``heartbeat_timeout`` elapses.  This loop
    emits periodic heartbeats for the whole segment.
    """

    while not stop.is_set():
        activity.heartbeat(details)
        try:
            await asyncio.wait_for(stop.wait(), timeout=ACTIVITY_HEARTBEAT_INTERVAL_SECONDS)
        except TimeoutError:
            continue


@dataclass
class ExecutionContext:
    """Activity dependencies, lazily built from settings unless overridden."""

    config: Settings | None = None
    repository: PostgresRepository | None = None
    core_runtime: CoreRuntime | None = None
    skill_provider: LocalSkillProvider | None = None

    def __post_init__(self) -> None:
        self.config = self.config or settings
        if self.repository is None:
            self.repository = build_repository(self.config)
        if self.core_runtime is None:
            self.core_runtime = build_core_runtime(self.config)
        if self.skill_provider is None:
            self.skill_provider = build_skill_provider(self.config)


_context: ExecutionContext | None = None


def set_execution_context(context: ExecutionContext) -> None:
    """Override activity dependencies (used by tests and embedded workers)."""

    global _context
    _context = context


def _get_context() -> ExecutionContext:
    global _context
    if _context is None:
        _context = ExecutionContext()
    return _context


def _event_to_state(conversation: Conversation, event: EventEnvelope) -> None:
    """Mirror the state transitions of ``append_claimed_event``."""

    if event.type == "run.started":
        conversation.run.state = ExecutionState.STARTING
    elif event.type == "run.running":
        conversation.run.state = ExecutionState.RUNNING
    elif event.type == "interaction.requested":
        conversation.run.state = ExecutionState.WAITING_INPUT
        conversation.run.pending_interaction = event.payload
    elif event.type in {"interaction.input", "approval.decided", "run.resuming"}:
        conversation.run.state = ExecutionState.RUNNING
        conversation.run.pending_interaction = None
    elif event.type == "run.completed":
        conversation.run.state = ExecutionState.COMPLETED
        conversation.run.pending_interaction = None
        conversation.run.result_summary = event.payload.get("result", event.payload)
    elif event.type == "run.failed":
        conversation.run.state = ExecutionState.FAILED
        conversation.run.pending_interaction = None
        conversation.run.error = event.payload
    elif event.type == "run.cancelled":
        conversation.run.state = ExecutionState.CANCELLED
        conversation.run.pending_interaction = None
    elif event.type == "run.lost":
        conversation.run.state = ExecutionState.LOST
        conversation.run.pending_interaction = None


def _make_sink(
    repository: PostgresRepository, conversation: Conversation
):
    async def sink(event: EventEnvelope) -> None:
        _event_to_state(conversation, event)
        event.seq = conversation.run.last_seq + 1
        repository.append_event(conversation, event)
        conversation.run.last_seq = event.seq

    return sink


def _build_run_request(
    request: dict,
    conversation: Conversation,
    session,
    workspace,
    ctx: ExecutionContext,
    *,
    runner_url: str | None = None,
) -> dict:
    """Build the runner request, mirroring the inline execution path."""

    skills = request.get("skills") or []
    runner_url = runner_url or _select_runner_url(ctx, session.tenant_id)
    workspace_ref = build_workspace_ref(
        ctx.config.core_runner_workspace_root if ctx.config else None,
        workspace.root_path,
    )
    tool_policy = request.get("tool_policy") or {}
    session_conversations = ctx.repository.list_conversations(session_id=session.id)
    silent_run_ids = {
        str(conv.run.run_id)
        for conv in session_conversations
        if getattr(conv, "mode", None) == ConversationMode.SILENT
        and conv.run is not None
        and conv.run.run_id is not None
    }
    injected_instructions = [
        {
            "type": "message",
            "payload": {"content": conv.task, "role": "user"},
            "source": "agentsupport",
            "conversation_id": str(conv.id),
            "occurred_at": conv.created_at.isoformat(),
        }
        for conv in sorted(
            (c for c in session_conversations if c.id != conversation.id),
            key=lambda c: c.created_at,
        )
        if getattr(conv, "mode", None) != ConversationMode.SILENT
    ]
    session_events = [
        event
        for event in ctx.repository.list_session_events(session.id)
        if not silent_run_ids or str(event.run_id) not in silent_run_ids
    ]
    recent_events = sorted(
        injected_instructions
        + [event.model_dump(mode="json") for event in session_events],
        key=lambda event: event.get("occurred_at") or "",
    )
    return {
        "run_id": str(conversation.run.run_id),
        "conversation_id": str(conversation.id),
        "session_id": str(session.id),
        "container_id": "temporal",
        "lease_epoch": session.lease_epoch,
        "fence_epoch": session.lease_epoch,
        "correlation_id": str(uuid4()),
        "context_bundle": {
            "task": conversation.task,
            "conversation_id": str(conversation.id),
            "workspace_ref": workspace_ref,
            "recent_events": recent_events,
            "skill_catalog": ctx.skill_provider.skill_catalog(
                skills, tenant_id=session.tenant_id
            ),
            "skill_package": ctx.skill_provider.skill_package(
                skills, tenant_id=session.tenant_id
            ),
            "tool_policy": tool_policy,
            "cli_policy": request.get("cli_policy") or {},
            "file_ref_format": bool(
                getattr(session.config, "file_ref_format", False)
            ),
            "mcp_refs": _resolve_mcp_refs(request.get("mcp_refs") or [], ctx),
        },
        "workspace_ref": workspace_ref,
        "tool_policy": tool_policy,
        "core_version": request.get("core_version", "0.1.0"),
        "runner_url": runner_url,
    }


def _resolve_mcp_refs(refs: list[dict], ctx: ExecutionContext) -> list[dict]:
    resolved: list[dict] = []
    for ref in refs:
        server_id = ref.get("server_id") if isinstance(ref, dict) else ref
        if not isinstance(server_id, str) or not server_id:
            continue
        server = ctx.repository.get_mcp_server(server_id)
        if server is None:
            continue
        resolved.append(
            {
                "server_id": server.server_id,
                "transport": server.transport,
                "http_url": server.http_url,
                "sse_url": server.sse_url,
                "headers": dict(server.headers),
                #: stdio 专用字段：少了它们，runner 拿到的就是"没有 command 的 stdio 配置"，
                #: vendored 客户端会抛 ValueError，而 discover_mcp_tools 把它**静默吞掉**
                #: ——表现就是"模型看不到任何 MCP 工具"。这条路径与
                #: skill_mcp_ops._resolve_mcp_refs 必须保持一致。
                "command": server.command,
                "args": list(server.args),
                "env": dict(server.env),
                "cwd": server.cwd,
                "description": server.description,
            }
        )
    return resolved


async def _after_segment(
    repository: PostgresRepository,
    core_runtime: CoreRuntime,
    conversation: Conversation,
) -> dict:
    persisted = repository.get_conversation(conversation.id)
    if persisted is None:
        raise RuntimeError("conversation disappeared after run segment")
    if persisted.run.state == ExecutionState.WAITING_INPUT:
        checkpoint = await core_runtime.checkpoint(
            persisted.run.run_id, "temporal_waiting_input"
        )
        repository.save_checkpoint(checkpoint)
        return {
            "status": "waiting",
            "checkpoint_id": str(checkpoint.checkpoint_id),
        }
    if persisted.run.state in TERMINAL_STATES:
        return {
            "status": "terminal",
            "state": persisted.run.state.value,
            "result": persisted.run.result_summary,
            "error": persisted.run.error,
        }
    raise RuntimeError(
        f"run segment returned without a durable state transition: "
        f"{persisted.run.state.value}"
    )


@activity.defn
async def start_runner(request: dict) -> dict:
    """Reserved: container lifecycle wiring lands in a later phase."""

    return {"status": "started", "run_id": request["run_id"]}


@activity.defn
async def stop_runner(payload: dict) -> dict:
    """Stop the runner and, when cancelled, record the terminal event."""

    ctx = _get_context()
    repository = ctx.repository
    request = payload["request"]
    if repository is None:
        raise RuntimeError("temporal mode requires a repository")
    if payload.get("terminal") == "cancelled":
        conversation = repository.get_conversation(UUID(request["conversation_id"]))
        if conversation is None:
            return {"status": "stopped", "run_id": request["run_id"]}
        if conversation.run.state != ExecutionState.CANCELLED:
            conversation.run.state = ExecutionState.CANCELLED
            event = EventEnvelope(
                event_id=uuid4(),
                run_id=conversation.run.run_id,
                seq=conversation.run.last_seq + 1,
                type="run.cancelled",
                payload={},
                source="agentsupport",
            )
            await _make_sink(repository, conversation)(event)
    return {"status": "stopped", "run_id": request["run_id"]}


@activity.defn
async def execute_run(request: dict) -> dict:
    """First run segment: drive the runner until a gate or a terminal state."""

    stop = asyncio.Event()
    heartbeat = asyncio.create_task(
        _heartbeat_until(stop, {"run_id": request["run_id"], "phase": "execute_run"})
    )
    try:
        ctx = _get_context()
        repository = ctx.repository
        core_runtime = ctx.core_runtime
        if repository is None or core_runtime is None:
            raise RuntimeError("temporal mode requires a repository and core runtime")
        conversation = repository.get_conversation(UUID(request["conversation_id"]))
        session = repository.get_session(UUID(request["session_id"]))
        workspace = repository.get_workspace(UUID(request["workspace_id"]))
        if conversation is None or session is None or workspace is None:
            raise RuntimeError("temporal run resources not found")
        runner_url = await _runner_url_for(ctx, session.tenant_id)
        run_request = _build_run_request(
            request, conversation, session, workspace, ctx, runner_url=runner_url
        )
        await core_runtime.run(run_request, _make_sink(repository, conversation))
        return await _after_segment(repository, core_runtime, conversation)
    finally:
        stop.set()
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat


@activity.defn
async def resume_run(payload: dict) -> dict:
    """Resume a run segment from a human gate with the delivered decision."""

    request = payload["request"]
    stop = asyncio.Event()
    heartbeat = asyncio.create_task(
        _heartbeat_until(stop, {"run_id": request["run_id"], "phase": "resume_run"})
    )
    try:
        ctx = _get_context()
        repository = ctx.repository
        core_runtime = ctx.core_runtime
        if repository is None or core_runtime is None:
            raise RuntimeError("temporal mode requires a repository and core runtime")
        conversation = repository.get_conversation(UUID(request["conversation_id"]))
        session = repository.get_session(UUID(request["session_id"]))
        workspace = repository.get_workspace(UUID(request["workspace_id"]))
        if conversation is None or session is None or workspace is None:
            raise RuntimeError("temporal run resources not found")
        checkpoint = repository.get_checkpoint(UUID(payload["checkpoint_id"]))
        if checkpoint is None:
            raise RuntimeError("checkpoint not found for resume")
        if "input" in payload:
            event_type = "interaction.input"
            value = payload["input"]["value"]
            decision = payload["input"]
        else:
            event_type = "approval.decided"
            value = payload["approval"]["decision"]
            decision = payload["approval"]
        conversation.run.state = ExecutionState.RUNNING
        decision_event = EventEnvelope(
            event_id=uuid4(),
            run_id=conversation.run.run_id,
            seq=conversation.run.last_seq + 1,
            type=event_type,
            payload=decision,
            source="agentsupport",
        )
        await _make_sink(repository, conversation)(decision_event)
        await core_runtime.resume(
            checkpoint,
            value,
            _make_sink(repository, conversation),
            runtime_context={
                "session_id": str(session.id),
                "container_id": "temporal",
                "fence_epoch": session.lease_epoch,
                "correlation_id": str(uuid4()),
            },
        )
        return await _after_segment(repository, core_runtime, conversation)
    finally:
        stop.set()
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat


@activity.defn
async def fail_run(payload: dict) -> dict:
    """Persist a terminal ``run.failed`` event when the workflow fails.

    Called from the workflow after segment retries are exhausted so a dead
    run never stays in ``RUNNING``/``STARTING`` forever in the control plane.
    """

    ctx = _get_context()
    repository = ctx.repository
    if repository is None:
        raise RuntimeError("temporal mode requires a repository")
    request = payload["request"]
    error = payload.get("error") or {}
    conversation = repository.get_conversation(UUID(request["conversation_id"]))
    if conversation is None:
        return {"status": "missing", "run_id": request["run_id"]}
    if conversation.run.state in TERMINAL_STATES:
        return {"status": "already_terminal", "run_id": request["run_id"]}
    event = EventEnvelope(
        event_id=uuid4(),
        run_id=conversation.run.run_id,
        seq=conversation.run.last_seq + 1,
        type="run.failed",
        payload={
            "code": str(error.get("code") or "WORKFLOW_FAILED"),
            "message": str(error.get("message") or "workflow failed"),
            # Only failures that carry the flag may be continued by the caller.
            "retryable": bool(error.get("retryable", False)),
        },
        source="agentsupport",
    )
    await _make_sink(repository, conversation)(event)
    return {"status": "failed", "run_id": request["run_id"]}

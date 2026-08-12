from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

from agent_runner_contracts.checkpoint import (
    Checkpoint,
    ContextBundle,
    tool_policy_hash,
    tool_versions_hash,
)
from agent_runner_contracts.execution import (
    ApprovalRequest,
    CheckpointRequest,
    CommandRequest,
    InputRequest,
    ResumeRequest,
    RunRequest,
)
from agent_runner_contracts.tools import (
    ApprovalDecision,
    AuthorizationStatus,
    ToolBatch,
    ToolDescriptor,
    ToolGatewayControlPlane,
    ToolGatewayPolicy,
)

from ...adapters.mcp import ControlledMcpProvider
from ...adapters.trae import AgentFactory, TraeExecutionAdapter, TraeRuntimeSettings
from ...application import RunRegistry
from ...domain import RunState
from ...mcp_runtime import build_mcp_provider, build_mcp_server_configs
from ...metrics import record_http, record_mcp_connection, render_metrics
from ...observability import configure_logging, run_context, set_correlation_id
from ...registration import (
    RunnerRegistrationClient,
    runner_registration_client_from_env,
)
from ...tools import ToolGatewayExecutor


def _validate_container_fence(request: RunRequest) -> None:
    expected_lease = os.getenv("SESSION_LEASE_EPOCH")
    if expected_lease is not None:
        try:
            lease_epoch = int(expected_lease)
        except ValueError as exc:
            raise HTTPException(503, "invalid session lease configuration") from exc
        if request.lease_epoch != lease_epoch:
            raise HTTPException(409, "session lease epoch mismatch")
        if request.fence_epoch != lease_epoch:
            raise HTTPException(409, "session fence epoch mismatch")
    elif request.fence_epoch not in {0, request.lease_epoch}:
        raise HTTPException(409, "session fence epoch mismatch")


def _validate_duplicate_run(existing: RunState, request: RunRequest) -> None:
    if (
        existing.request.session_id != request.session_id
        or existing.request.container_id != request.container_id
        or existing.request.lease_epoch != request.lease_epoch
        or (request.fence_epoch != 0 and existing.request.fence_epoch != request.fence_epoch)
    ):
        raise HTTPException(409, "run lease fence mismatch")


ToolHandler = Callable[[dict[str, Any]], Awaitable[Any]]


async def _echo_tool(arguments: dict[str, Any]) -> Any:
    return arguments.get("value")


def _tool_executor(
    request: RunRequest,
    handlers: dict[str, ToolHandler] | None,
    mcp_provider: ControlledMcpProvider | None,
) -> ToolGatewayExecutor:
    raw_descriptors = request.tool_policy.get("tools", [{"name": "echo"}])
    descriptors = [ToolDescriptor.model_validate(item) for item in raw_descriptors]
    mcp_refs = [
        item.get("server_id") if isinstance(item, dict) else str(item)
        for item in request.context_bundle.get("mcp_refs", [])
    ]
    if mcp_provider:
        descriptors.extend(mcp_provider.descriptors(mcp_refs))
    allowed = set(request.tool_policy.get("allowed_tools", [item.name for item in descriptors]))
    policy = ToolGatewayPolicy(
        allowed_tools=allowed,
        approval_required_tools=set(request.tool_policy.get("approval_required_tools", [])),
    )
    tool_handlers = {"echo": _echo_tool}
    if handlers:
        tool_handlers.update(handlers)
    if mcp_provider:
        tool_handlers.update(mcp_provider.handlers(mcp_refs))
    return ToolGatewayExecutor(ToolGatewayControlPlane(descriptors), policy, tool_handlers)


async def _execute_tool_batch(state: RunState, decision: ApprovalDecision | None = None) -> None:
    assert state.tool_executor is not None
    assert state.tool_batch is not None
    results = await state.tool_executor.execute(
        state.tool_batch, state.tool_authorization, decision
    )
    for result in results:
        state.emit("tool.result", result.model_dump(mode="json"))
    state.status = "COMPLETED"
    state.pending_interaction = None
    state.emit("run.completed", {"result": {"status": "completed", "tool_calls": len(results)}})


def create_runner_app(
    tool_handlers: dict[str, ToolHandler] | None = None,
    mcp_provider: ControlledMcpProvider | None = None,
    *,
    runner_mode: str | None = None,
    trae_settings: TraeRuntimeSettings | None = None,
    trae_agent_factory: AgentFactory | None = None,
    registration_client: RunnerRegistrationClient | None = None,
) -> FastAPI:
    configure_logging()
    runs = RunRegistry()
    mode = runner_mode or os.getenv("SESSION_RUNNER_MODE", "deterministic")
    selected_registration = registration_client or runner_registration_client_from_env(mode=mode)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        if selected_registration is not None:
            await selected_registration.register()
            selected_registration.start()
        try:
            yield
        finally:
            if selected_registration is not None:
                await selected_registration.stop()

    app = FastAPI(
        title="Session Core Runner",
        version="0.2.0",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def observability_middleware(request, call_next):
        route = request.scope.get("route")
        route_path = getattr(route, "path", None) or request.url.path
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception:
            status = 500
            raise
        finally:
            record_http(request.method, route_path, status)
        return response

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics() -> str:
        body, _content_type = render_metrics()
        return body.decode("utf-8")

    def create_trae_execution(
        state: RunState,
        mcp_servers_config: dict[str, dict[str, Any]] | None = None,
    ) -> TraeExecutionAdapter:
        def on_waiting(interaction: dict[str, Any], batch: ToolBatch, next_step: int) -> None:
            state.pending_interaction = interaction
            state.tool_batch = batch
            state.status = "WAITING_INPUT"
            state.status_changed.set()

        return TraeExecutionAdapter(
            state.request,
            state.emit,
            on_waiting,
            settings=trae_settings,
            agent_factory=trae_agent_factory,
            mcp_servers_config=mcp_servers_config,
        )

    async def close_mcp(state: RunState) -> None:
        provider = state.mcp_provider
        if provider is not None:
            state.mcp_provider = None
            await provider.close()

    async def drive_trae(
        state: RunState,
        checkpoint: Checkpoint | None = None,
        decision: ApprovalDecision | None = None,
    ) -> None:
        try:
            assert state.trae_execution is not None
            if checkpoint is None:
                result = await state.trae_execution.run()
            else:
                assert decision is not None
                result = await state.trae_execution.resume(checkpoint, decision)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - core failures become standard events
            state.status = "FAILED"
            state.pending_interaction = None
            state.emit(
                "run.failed",
                {"code": "TRAE_RUNTIME_ERROR", "message": str(exc)},
            )
        else:
            state.status = "COMPLETED"
            state.pending_interaction = None
            state.emit("run.completed", {"result": result})
        finally:
            state.status_changed.set()

    @app.get("/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> dict[str, str]:
        return {"status": "ready", "mode": mode}

    @app.post("/runs")
    async def start_run(request: RunRequest):
        _validate_container_fence(request)
        set_correlation_id(request.correlation_id)
        run_context(
            run_id=str(request.run_id),
            session_id=str(request.session_id),
            conversation_id=str(request.conversation_id),
        ).__enter__()
        if request.run_id in runs:
            state = runs[request.run_id]
            _validate_duplicate_run(state, request)
            return {
                "run_id": request.run_id,
                "status": state.status,
                "events": state.events,
            }
        state = RunState(request)
        runs[request.run_id] = state
        state.emit("run.started", {"conversation_id": str(request.conversation_id)})
        task = str(request.context_bundle.get("task", ""))
        raw_batch = request.context_bundle.get("tool_batch")
        mcp_refs = request.context_bundle.get("mcp_refs") or []
        mcp_servers_config = build_mcp_server_configs(mcp_refs)
        if mode == "trae":
            state.status = "RUNNING"
            state.trae_execution = create_trae_execution(state, mcp_servers_config)
            state.background = asyncio.create_task(drive_trae(state))
            await state.status_changed.wait()
        elif raw_batch is not None:
            try:
                dynamic_refs = [ref for ref in mcp_refs if isinstance(ref, dict)]
                if dynamic_refs:
                    try:
                        state.mcp_provider = await build_mcp_provider(dynamic_refs)
                    except Exception:
                        record_mcp_connection("failed")
                        raise
                    record_mcp_connection(
                        "connected" if state.mcp_provider is not None else "no_tools"
                    )
                state.tool_executor = _tool_executor(
                    request, tool_handlers, state.mcp_provider or mcp_provider
                )
                state.tool_batch = ToolBatch.model_validate(raw_batch)
                state.tool_authorization = state.tool_executor.authorize(state.tool_batch)
                state.emit(
                    "tool.authorization",
                    state.tool_authorization.model_dump(mode="json"),
                )
                if state.tool_authorization.status == AuthorizationStatus.REQUIRES_APPROVAL:
                    state.status = "WAITING_INPUT"
                    state.pending_interaction = {
                        "interaction_id": state.tool_authorization.approval_id,
                        "kind": "approval",
                        "tool_batch_hash": state.tool_batch.batch_hash,
                        "tool_batch": state.tool_batch.model_dump(mode="json"),
                        "tool_policy": state.request.tool_policy,
                    }
                    state.emit("interaction.requested", state.pending_interaction)
                else:
                    await _execute_tool_batch(state)
                    await close_mcp(state)
            except Exception as exc:  # noqa: BLE001 - tool errors become run failures
                state.status = "FAILED"
                state.emit("run.failed", {"code": "TOOL_GATEWAY_ERROR", "message": str(exc)})
                await close_mcp(state)
        elif task.startswith("ask:"):
            state.status = "WAITING_INPUT"
            state.pending_interaction = {"interaction_id": uuid4().hex, "question": task[4:]}
            state.emit("interaction.requested", state.pending_interaction)
        else:
            state.status = "COMPLETED"
            state.emit("message", {"content": f"completed: {task}"})
            state.emit("run.completed", {"result": {"status": "completed"}})
        return {"run_id": request.run_id, "status": state.status, "events": state.events}

    @app.post("/runs/{run_id}/input")
    async def accept_input(run_id: UUID, request: InputRequest):
        state = runs.get(run_id)
        if not state:
            raise HTTPException(404, "run not found")
        cached = state.cached_command(request.command_id)
        if cached is not None:
            return cached
        if state.status != "WAITING_INPUT" or not state.pending_interaction:
            raise HTTPException(409, "run is not waiting for input")
        if request.interaction_id != state.pending_interaction["interaction_id"]:
            raise HTTPException(409, "interaction mismatch")
        if state.pending_interaction.get("kind") == "approval":
            raise HTTPException(409, "run requires approval")
        first_new_event = len(state.events)
        state.pending_interaction = None
        state.status = "COMPLETED"
        state.emit("message", {"input": request.value})
        state.emit("run.completed", {"result": {"status": "completed"}})
        result = {
            "run_id": run_id,
            "status": state.status,
            "events": state.events[first_new_event:],
        }
        state.remember_command(request.command_id, result)
        await close_mcp(state)
        return result

    @app.post("/runs/{run_id}/approval")
    async def accept_approval(run_id: UUID, request: ApprovalRequest):
        state = runs.get(run_id)
        if not state:
            raise HTTPException(404, "run not found")
        cached = state.cached_command(request.command_id)
        if cached is not None:
            return cached
        if state.status != "WAITING_INPUT" or not state.pending_interaction:
            raise HTTPException(409, "run is not waiting for approval")
        if request.approval_id != state.pending_interaction["interaction_id"]:
            raise HTTPException(409, "approval mismatch")
        if request.decision not in {"APPROVE_ONCE", "REJECT"}:
            raise HTTPException(422, "invalid approval decision")
        first_new_event = len(state.events)
        if state.trae_execution is not None:
            state.status = "RUNNING"
            state.pending_interaction = None
            state.status_changed.clear()
            try:
                state.trae_execution.approve(ApprovalDecision(request.decision))
            except RuntimeError as exc:
                raise HTTPException(409, str(exc)) from exc
            await state.status_changed.wait()
        elif state.tool_batch is not None:
            await _execute_tool_batch(state, ApprovalDecision(request.decision))
        else:
            state.pending_interaction = None
            state.status = "COMPLETED"
            state.emit(
                "run.completed",
                {"result": {"status": "completed", "approval": request.decision}},
            )
        result = {
            "run_id": run_id,
            "status": state.status,
            "events": state.events[first_new_event:],
        }
        state.remember_command(request.command_id, result)
        await close_mcp(state)
        return result

    @app.post("/runs/{run_id}/checkpoint")
    async def checkpoint(run_id: UUID, request: CheckpointRequest):
        state = runs.get(run_id)
        if not state:
            raise HTTPException(404, "run not found")
        tool_policy = dict(state.request.tool_policy)
        if state.pending_interaction:
            tool_policy.update(state.pending_interaction.get("tool_policy", {}))
        pending_tool_calls = (
            [call.model_dump(mode="json") for call in state.tool_batch.calls]
            if state.tool_batch is not None
            else []
        )
        tool_batch_hash = state.tool_batch.batch_hash if state.tool_batch is not None else None
        if state.tool_batch is not None:
            tool_policy["tool_batch"] = state.tool_batch.model_dump(mode="json")
            tool_policy["tool_batch_hash"] = tool_batch_hash
            tool_policy["pending_tool_calls"] = pending_tool_calls
        if state.trae_execution is not None:
            tool_policy.update(state.trae_execution.checkpoint_policy())
        tool_policy["command_results"] = state.command_results
        checkpoint_tool_policy_hash = tool_policy_hash(tool_policy)
        raw_tools = list(tool_policy.get("tools", []))
        checkpoint_tool_versions_hash = tool_versions_hash(raw_tools) if raw_tools else None
        tool_policy["tool_policy_hash"] = checkpoint_tool_policy_hash
        if checkpoint_tool_versions_hash:
            tool_policy["tool_versions_hash"] = checkpoint_tool_versions_hash
        context = ContextBundle(
            task=str(state.request.context_bundle.get("task", "")),
            conversation_id=state.request.conversation_id,
            workspace_ref=state.request.workspace_ref,
            recent_events=[event.model_dump(mode="json") for event in state.events],
            skill_manifest=list(state.request.context_bundle.get("skill_manifest", [])),
            mcp_refs=list(state.request.context_bundle.get("mcp_refs", [])),
            tool_policy=tool_policy,
        )
        context_hash = hashlib.sha256(
            json.dumps(
                context.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        result = Checkpoint(
            conversation_id=state.request.conversation_id,
            run_id=run_id,
            last_event_seq=len(state.events),
            context_bundle=context,
            pending_interaction=state.pending_interaction,
            pending_tool_calls=pending_tool_calls,
            tool_batch_hash=tool_batch_hash,
            tool_policy_hash=checkpoint_tool_policy_hash,
            tool_versions_hash=checkpoint_tool_versions_hash,
            workspace_ref=state.request.workspace_ref,
            workspace_write_lease_epoch=state.request.lease_epoch,
            core_type="trae",
            core_version=state.request.core_version,
            context_bundle_hash=context_hash,
        )
        state.emit(
            "checkpoint.created",
            {"checkpoint_id": str(result.checkpoint_id), "reason": request.reason},
        )
        return result

    @app.post("/runs/{run_id}/cancel")
    async def cancel(run_id: UUID, request: CommandRequest | None = None):
        state = runs.get(run_id)
        if not state:
            raise HTTPException(404, "run not found")
        command_id = request.command_id if request else None
        cached = state.cached_command(command_id)
        if cached is not None:
            return cached
        if state.status == "CANCELLED":
            result = {"run_id": run_id, "status": state.status, "events": []}
            state.remember_command(command_id, result)
            return result
        first_new_event = len(state.events)
        if state.background is not None and not state.background.done():
            state.background.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await state.background
        state.status = "CANCELLED"
        state.pending_interaction = None
        state.emit("run.cancelled")
        result = {
            "run_id": run_id,
            "status": state.status,
            "events": state.events[first_new_event:],
        }
        state.remember_command(command_id, result)
        await close_mcp(state)
        return result

    @app.post("/runs/{run_id}/resume")
    async def resume(run_id: UUID, request: ResumeRequest):
        checkpoint = Checkpoint.model_validate(request.checkpoint)
        first_new_event = len(runs[run_id].events) if run_id in runs else 0
        state = runs.get(run_id)
        if state is None:
            run_request = RunRequest(
                run_id=run_id,
                conversation_id=checkpoint.conversation_id,
                session_id=request.session_id or uuid4(),
                container_id=request.container_id or "restored",
                lease_epoch=request.lease_epoch or checkpoint.workspace_write_lease_epoch,
                fence_epoch=request.fence_epoch or 0,
                correlation_id=request.correlation_id or "resume",
                context_bundle=checkpoint.context_bundle.model_dump(mode="json"),
                workspace_ref=checkpoint.workspace_ref,
                tool_policy=checkpoint.context_bundle.tool_policy,
                core_version=checkpoint.core_version,
            )
            _validate_container_fence(run_request)
            state = RunState(run_request)
            runs[run_id] = state
            state.command_results = dict(
                checkpoint.context_bundle.tool_policy.get("command_results", {})
            )
        cached = state.cached_command(request.command_id)
        if cached is not None:
            return cached
        resume_mcp_refs = getattr(checkpoint.context_bundle, "mcp_refs", None) or []
        resume_mcp_servers_config = build_mcp_server_configs(resume_mcp_refs)
        if mode == "trae" and checkpoint.pending_tool_calls:
            try:
                decision = ApprovalDecision(str(request.value))
            except ValueError as exc:
                raise HTTPException(422, "tool batch resume requires an approval decision") from exc
            state.emit("checkpoint.restored", {"checkpoint_id": str(checkpoint.checkpoint_id)})
            state.pending_interaction = None
            state.status = "RUNNING"
            state.status_changed.clear()
            if state.trae_execution is not None and state.background is not None:
                try:
                    state.trae_execution.approve(decision)
                except RuntimeError as exc:
                    raise HTTPException(409, str(exc)) from exc
            else:
                state.trae_execution = create_trae_execution(
                    state, resume_mcp_servers_config
                )
                state.background = asyncio.create_task(
                    drive_trae(state, checkpoint=checkpoint, decision=decision)
                )
            await state.status_changed.wait()
            result = {
                "run_id": run_id,
                "status": state.status,
                "events": state.events[first_new_event:],
            }
            state.remember_command(request.command_id, result)
            await close_mcp(state)
            return result
        raw_batch = None
        if checkpoint.pending_interaction:
            raw_batch = checkpoint.pending_interaction.get("tool_batch")
        if raw_batch is None and checkpoint.pending_tool_calls:
            raw_batch = {"calls": checkpoint.pending_tool_calls}
        if raw_batch is not None:
            batch = ToolBatch.model_validate(raw_batch)
            if checkpoint.tool_batch_hash and batch.batch_hash != checkpoint.tool_batch_hash:
                raise HTTPException(409, "tool batch hash mismatch")
            resume_dynamic_refs = [
                ref for ref in resume_mcp_refs if isinstance(ref, dict)
            ]
            if resume_dynamic_refs:
                try:
                    state.mcp_provider = await build_mcp_provider(resume_dynamic_refs)
                except Exception:
                    record_mcp_connection("failed")
                    raise
                record_mcp_connection(
                    "connected" if state.mcp_provider is not None else "no_tools"
                )
            state.tool_executor = _tool_executor(
                state.request, tool_handlers, state.mcp_provider or mcp_provider
            )
            state.tool_batch = batch
            state.tool_authorization = state.tool_executor.authorize(batch)
        state.emit("checkpoint.restored", {"checkpoint_id": str(checkpoint.checkpoint_id)})
        state.pending_interaction = None
        if state.tool_batch is not None:
            try:
                decision = ApprovalDecision(str(request.value))
            except ValueError as exc:
                raise HTTPException(422, "tool batch resume requires an approval decision") from exc
            await _execute_tool_batch(state, decision)
        else:
            state.status = "COMPLETED"
            state.emit("message", {"input": request.value})
            state.emit("run.completed", {"result": {"status": "completed", "resumed": True}})
        result = {
            "run_id": run_id,
            "status": state.status,
            "events": state.events[first_new_event:],
        }
        state.remember_command(request.command_id, result)
        await close_mcp(state)
        return result

    @app.get("/runs/{run_id}/events")
    async def events(run_id: UUID, after_seq: int = 0):
        state = runs.get(run_id)
        if not state:
            raise HTTPException(404, "run not found")
        return [event for event in state.events if event.seq > after_seq]

    return app


app = create_runner_app()

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from agent_runner_contracts.checkpoint import tool_policy_hash, tool_versions_hash
from agent_runner_contracts.tools import (
    ApprovalDecision,
    AuthorizationStatus,
    ToolBatch,
    ToolCall,
    ToolDescriptor,
    ToolGatewayControlPlane,
    ToolGatewayPolicy,
)

EventEmitter = Callable[[str, dict[str, Any]], None]
WaitingCallback = Callable[[dict[str, Any], ToolBatch, int], None]
AgentFactory = Callable[["TraeRuntimeSettings", Any, Path], Any]


SIDE_EFFECT_TOOLS = {
    "bash",
    "json_edit_tool",
    "str_replace_based_edit_tool",
}


@dataclass(frozen=True)
class TraeRuntimeSettings:
    config_path: Path
    provider: str
    model: str
    model_base_url: str | None
    api_key: str
    max_steps: int
    workspace_roots: tuple[Path, ...]
    system_prompt_file: Path | None = None

    @classmethod
    def from_environment(cls) -> TraeRuntimeSettings:
        provider = os.getenv("TRAE_PROVIDER", "anthropic")
        api_key = os.getenv("TRAE_API_KEY") or os.getenv(f"{provider.upper()}_API_KEY", "")
        roots = tuple(
            Path(value).resolve()
            for value in os.getenv(
                "SESSION_RUNNER_WORKSPACE_ROOTS", "/workspace,/workspace-data"
            ).split(",")
            if value.strip()
        )
        prompt_file = os.getenv("SESSION_RUNNER_TRAE_PROMPT_FILE")
        default_config = Path(__file__).resolve().parents[1] / "trae_config.yaml"
        return cls(
            config_path=Path(os.getenv("SESSION_RUNNER_TRAE_CONFIG", str(default_config))),
            provider=provider,
            model=os.getenv("TRAE_MODEL", "claude-sonnet-4-20250514"),
            model_base_url=os.getenv("TRAE_MODEL_BASE_URL") or None,
            api_key=api_key,
            max_steps=int(os.getenv("TRAE_MAX_STEPS", "8")),
            workspace_roots=roots,
            system_prompt_file=Path(prompt_file) if prompt_file else None,
        )

    def validate(self) -> None:
        if self.config_path.suffix not in {".yaml", ".yml"}:
            raise ValueError("Trae config must use a .yaml or .yml extension")
        if not self.config_path.is_file():
            raise ValueError(f"Trae config does not exist: {self.config_path}")
        if not self.api_key:
            raise ValueError("Trae API key is not configured in the runner environment")

    def workspace(self, value: str) -> Path:
        path = Path(value).resolve()
        if not any(path == root or path.is_relative_to(root) for root in self.workspace_roots):
            raise ValueError("workspace_ref is outside the runner workspace roots")
        if not path.is_dir():
            raise ValueError(f"workspace_ref does not exist: {path}")
        return path


def _ensure_vendored_trae_path() -> None:
    configured = os.getenv("SESSION_RUNNER_TRAE_VENDOR_PATH")
    candidates = [
        Path(configured) if configured else None,
        Path.cwd() / "vendor" / "trae-agent-src",
        *(
            parent / "vendor" / "trae-agent-src"
            for parent in Path(__file__).resolve().parents
        ),
    ]
    for vendor in candidates:
        if vendor is not None and vendor.is_dir():
            resolved = str(vendor.resolve())
            if resolved not in sys.path:
                sys.path.insert(0, resolved)
            return


def _resolve_system_prompt(settings: TraeRuntimeSettings, request: Any) -> str | None:
    """Resolve a custom system prompt for the Trae agent.

    Precedence: an explicitly configured prompt file
    (``SESSION_RUNNER_TRAE_PROMPT_FILE``) wins, then the ``system_prompt``
    field carried by the request context bundle (so preset/project config can
    inject a prompt). Returns ``None`` to fall back to the built-in prompt.
    """
    if settings.system_prompt_file is not None:
        path = settings.system_prompt_file.resolve()
        if not path.is_file():
            raise ValueError(f"Trae system prompt file does not exist: {path}")
        return path.read_text(encoding="utf-8")
    bundle = getattr(request, "context_bundle", None)
    if isinstance(bundle, dict):
        prompt = bundle.get("system_prompt")
        if isinstance(prompt, str) and prompt.strip():
            return prompt
    return None


def _default_agent_factory(settings: TraeRuntimeSettings, request: Any, trajectory: Path) -> Any:
    _ensure_vendored_trae_path()
    from trae_agent.agent.agent import Agent
    from trae_agent.utils.config import Config

    config = Config.create(config_file=str(settings.config_path)).resolve_config_values(
        provider=settings.provider,
        model=settings.model,
        model_base_url=settings.model_base_url,
        api_key=settings.api_key,
        max_steps=settings.max_steps,
    )
    agent = Agent("trae_agent", config, str(trajectory))
    system_prompt = _resolve_system_prompt(settings, request)
    if system_prompt is not None:
        agent.agent._system_prompt = system_prompt
    return agent


def _trae_tool_result(
    *,
    call_id: str,
    name: str,
    success: bool,
    result: str | None = None,
    error: str | None = None,
    tool_id: str | None = None,
) -> Any:
    _ensure_vendored_trae_path()
    from trae_agent.tools.base import ToolResult

    return ToolResult(
        call_id=call_id,
        id=tool_id,
        name=name,
        success=success,
        result=result,
        error=error,
    )


class TraeToolGatewayBridge:
    """Trae ToolExecutor wrapper that enforces AgentSupport batch authorization."""

    def __init__(
        self,
        delegate: Any,
        tool_names: list[str],
        raw_policy: dict[str, Any],
        emit: EventEmitter,
        on_waiting: WaitingCallback,
        next_step: Callable[[], int],
    ) -> None:
        self.delegate = delegate
        self.emit = emit
        self.on_waiting = on_waiting
        self.next_step = next_step
        configured = {
            item["name"]: ToolDescriptor.model_validate(item)
            for item in raw_policy.get("tools", [])
        }
        descriptors = [
            configured.get(
                name,
                ToolDescriptor(
                    name=name,
                    requires_approval=name in SIDE_EFFECT_TOOLS or name.startswith("mcp."),
                ),
            )
            for name in tool_names
        ]
        allowed = set(raw_policy.get("allowed_tools", tool_names))
        approvals = set(
            raw_policy.get("approval_required_tools", SIDE_EFFECT_TOOLS.intersection(allowed))
        )
        self.control_plane = ToolGatewayControlPlane(descriptors)
        self.policy = ToolGatewayPolicy(
            allowed_tools=allowed,
            approval_required_tools=approvals,
        )
        self.pending_batch: ToolBatch | None = None
        self.pending_authorization: Any = None
        self.pending_calls: list[Any] = []
        self.pending_method: str | None = None
        self._decision: asyncio.Future[ApprovalDecision] | None = None
        self._results: dict[str, Any] = {}

    async def close_tools(self) -> Any:
        return await self.delegate.close_tools()

    async def sequential_tool_call(self, calls: list[Any]) -> list[Any]:
        return await self._execute(calls, "sequential_tool_call")

    async def parallel_tool_call(self, calls: list[Any]) -> list[Any]:
        return await self._execute(calls, "parallel_tool_call")

    def approve(self, decision: ApprovalDecision) -> None:
        if self._decision is None or self._decision.done():
            raise RuntimeError("Trae run is not waiting for tool approval")
        self._decision.set_result(decision)

    async def execute_restored(
        self, calls: list[Any], method: str, decision: ApprovalDecision
    ) -> list[Any]:
        return await self._execute(calls, method, restored_decision=decision)

    async def _execute(
        self,
        calls: list[Any],
        method: str,
        restored_decision: ApprovalDecision | None = None,
    ) -> list[Any]:
        batch = ToolBatch(
            calls=[
                ToolCall(call_id=call.call_id, name=call.name, arguments=dict(call.arguments))
                for call in calls
            ]
        )
        for call in batch.calls:
            self.emit("tool.call", call.model_dump(mode="json"))
        authorization = self.control_plane.authorize(batch, self.policy)
        self.emit("tool.authorization", authorization.model_dump(mode="json"))
        decision = restored_decision
        if authorization.status == AuthorizationStatus.REQUIRES_APPROVAL and decision is None:
            self.pending_batch = batch
            self.pending_authorization = authorization
            self.pending_calls = calls
            self.pending_method = method
            self._decision = asyncio.get_running_loop().create_future()
            interaction = {
                "interaction_id": authorization.approval_id,
                "kind": "approval",
                "tool_batch_hash": batch.batch_hash,
                "tool_batch": batch.model_dump(mode="json"),
                "pending_tool_calls": [asdict(call) for call in calls],
                "tool_policy": {
                    "allowed_tools": sorted(self.policy.allowed_tools),
                    "approval_required_tools": sorted(self.policy.approval_required_tools),
                    "tools": [
                        item.model_dump(mode="json") for item in self.control_plane.describe_tools()
                    ],
                },
                "tool_method": method,
                "next_step": self.next_step(),
            }
            self.emit("interaction.requested", interaction)
            self.on_waiting(interaction, batch, interaction["next_step"])
            decision = await self._decision
        if authorization.status == AuthorizationStatus.DENIED:
            return self._rejected(calls, authorization.reason or "tool authorization denied")
        if authorization.status == AuthorizationStatus.REQUIRES_APPROVAL:
            if decision == ApprovalDecision.REJECT:
                return self._rejected(calls, "tool batch rejected by user")
            if decision != ApprovalDecision.APPROVE_ONCE:
                raise RuntimeError("tool batch approval decision is missing")
        results = await getattr(self.delegate, method)(calls)
        unique_results: list[Any] = []
        for result in results:
            cached = self._results.get(result.call_id)
            if cached is None:
                self._results[result.call_id] = result
                cached = result
            unique_results.append(cached)
            self.emit(
                "tool.result",
                {
                    "call_id": cached.call_id,
                    "name": cached.name,
                    "success": cached.success,
                    "output": cached.result,
                    "error": cached.error,
                },
            )
        self._clear_pending()
        return unique_results

    def _rejected(self, calls: list[Any], reason: str) -> list[Any]:
        results = [
            _trae_tool_result(
                call_id=call.call_id,
                tool_id=getattr(call, "id", None),
                name=call.name,
                success=False,
                error=reason,
            )
            for call in calls
        ]
        for result in results:
            self.emit(
                "tool.result",
                {
                    "call_id": result.call_id,
                    "name": result.name,
                    "success": False,
                    "output": None,
                    "error": reason,
                },
            )
        self._clear_pending()
        return results

    def _clear_pending(self) -> None:
        self.pending_batch = None
        self.pending_authorization = None
        self.pending_calls = []
        self.pending_method = None
        self._decision = None


class TraeExecutionAdapter:
    """Owns one real Trae Agent instance inside a Session Runner process."""

    def __init__(
        self,
        request: Any,
        emit: EventEmitter,
        on_waiting: WaitingCallback,
        *,
        settings: TraeRuntimeSettings | None = None,
        agent_factory: AgentFactory | None = None,
        mcp_servers_config: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.request = request
        self.emit = emit
        self.on_waiting = on_waiting
        self.settings = settings or TraeRuntimeSettings.from_environment()
        self.agent_factory = agent_factory or _default_agent_factory
        self.mcp_servers_config = dict(mcp_servers_config or {})
        self.workspace: Path | None = None
        self.trajectory: Path | None = None
        self.agent: Any = None
        self.bridge: TraeToolGatewayBridge | None = None
        self.next_step = 1

    def approve(self, decision: ApprovalDecision) -> None:
        if self.bridge is None:
            raise RuntimeError("Trae tool gateway is not initialized")
        self.bridge.approve(decision)

    def checkpoint_policy(self) -> dict[str, Any]:
        return {
            "next_step": self.next_step,
            "trajectory_ref": str(self.trajectory) if self.trajectory else None,
        }

    async def run(self) -> dict[str, Any]:
        await self._initialize_agent()
        execution = await self.agent.run(
            self.request.context_bundle.get("task", ""),
            {"project_path": str(self.workspace)},
        )
        self._emit_trajectory()
        if not execution.success:
            step_error = execution.steps[-1].error if execution.steps else None
            raise RuntimeError(step_error or execution.final_result or "Trae execution failed")
        result = {
            "status": "completed",
            "content": execution.final_result,
            "steps": len(execution.steps),
        }
        if execution.final_result:
            self.emit("message", {"content": execution.final_result})
        return result

    async def resume(self, checkpoint: Any, decision: ApprovalDecision) -> dict[str, Any]:
        await self._initialize_agent()
        if self.bridge is None:
            raise RuntimeError("Trae tool gateway is not initialized")
        current_policy = {
            "allowed_tools": sorted(self.bridge.policy.allowed_tools),
            "approval_required_tools": sorted(self.bridge.policy.approval_required_tools),
        }
        if checkpoint.tool_policy_hash and checkpoint.tool_policy_hash != tool_policy_hash(
            current_policy
        ):
            raise ValueError("tool policy hash mismatch")
        current_tools = [
            item.model_dump(mode="json") for item in self.bridge.control_plane.describe_tools()
        ]
        if checkpoint.tool_versions_hash and checkpoint.tool_versions_hash != tool_versions_hash(
            current_tools
        ):
            raise ValueError("tool versions hash mismatch")
        pending = checkpoint.pending_interaction or {}
        raw_calls = pending.get("pending_tool_calls") or checkpoint.pending_tool_calls
        if not raw_calls:
            raise ValueError("Trae checkpoint does not contain pending tool calls")
        batch = ToolBatch(calls=[ToolCall.model_validate(call) for call in raw_calls])
        if checkpoint.tool_batch_hash and checkpoint.tool_batch_hash != batch.batch_hash:
            raise ValueError("tool batch hash mismatch")

        _ensure_vendored_trae_path()
        from trae_agent.agent.agent_basics import (
            AgentExecution,
            AgentState,
            AgentStep,
            AgentStepState,
        )
        from trae_agent.tools.base import ToolCall as TraeToolCall
        from trae_agent.utils.llm_clients.llm_basics import LLMMessage

        task = checkpoint.context_bundle.task
        self.agent.agent.new_task(
            task,
            {"project_path": str(self.workspace)},
        )
        calls = [TraeToolCall(**call) for call in raw_calls]
        method = pending.get("tool_method", "sequential_tool_call")
        tool_results = await self.bridge.execute_restored(calls, method, decision)
        messages = list(self.agent.agent.initial_messages)
        messages.extend(LLMMessage(role="user", tool_result=result) for result in tool_results)
        execution = AgentExecution(task=task, steps=[], agent_state=AgentState.RUNNING)
        self.next_step = int(pending.get("next_step", 2))
        for step_number in range(self.next_step, self.agent.agent.max_steps + 1):
            self.next_step = step_number + 1
            step = AgentStep(step_number=step_number, state=AgentStepState.THINKING)
            messages = await self.agent.agent._run_llm_step(step, messages, execution)
            await self.agent.agent._finalize_step(step, messages, execution)
            if execution.agent_state == AgentState.COMPLETED:
                break
        await self.agent.agent._close_tools()
        self._emit_trajectory()
        if not execution.success:
            step_error = execution.steps[-1].error if execution.steps else None
            raise RuntimeError(step_error or execution.final_result or "Trae resume failed")
        if execution.final_result:
            self.emit("message", {"content": execution.final_result})
        return {
            "status": "completed",
            "content": execution.final_result,
            "steps": len(execution.steps),
            "resumed": True,
        }

    async def _initialize_agent(self) -> None:
        self.settings.validate()
        self.workspace = self.settings.workspace(self.request.workspace_ref)
        trajectory_dir = self.workspace / ".agentsupport" / "trajectories"
        trajectory_dir.mkdir(parents=True, exist_ok=True)
        self.trajectory = trajectory_dir / f"{self.request.run_id}.json"
        self.agent = self.agent_factory(self.settings, self.request, self.trajectory)
        if self.mcp_servers_config:
            self.agent.agent.mcp_servers_config = dict(self.mcp_servers_config)
            self.agent.agent.allow_mcp_servers = list(self.mcp_servers_config)
            await self.agent.agent.initialise_mcp()
            # The base Agent.run would re-initialise MCP; discovery already ran.
            self.agent.agent.allow_mcp_servers = []
        tool_names = [tool.name for tool in self.agent.agent.tools]
        original = self.agent.agent._tool_caller
        self.bridge = TraeToolGatewayBridge(
            original,
            tool_names,
            self.request.tool_policy,
            self.emit,
            self._waiting,
            self._next_step,
        )
        mcp_names = {name for name in tool_names if name.startswith("mcp.")}
        if mcp_names:
            self.bridge.policy.allowed_tools.update(mcp_names)
        self.agent.agent._tool_caller = self.bridge

    def _next_step(self) -> int:
        if not self.trajectory or not self.trajectory.is_file():
            return 2
        try:
            payload = json.loads(self.trajectory.read_text(encoding="utf-8"))
            return len(payload.get("agent_steps", [])) + 2
        except (OSError, ValueError, TypeError):
            return 2

    def _waiting(self, interaction: dict[str, Any], batch: ToolBatch, next_step: int) -> None:
        self.next_step = next_step
        self.on_waiting(interaction, batch, next_step)

    def _emit_trajectory(self) -> None:
        if not self.trajectory or not self.trajectory.is_file():
            self.emit("trajectory.missing", {"path": str(self.trajectory)})
            return
        raw = self.trajectory.read_bytes()
        try:
            payload = json.loads(raw)
            steps = len(payload.get("agent_steps", []))
        except (ValueError, TypeError):
            steps = 0
        self.emit(
            "trajectory.recorded",
            {
                "path": str(self.trajectory),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "steps": steps,
            },
        )

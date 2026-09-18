from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

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

from ..security import (
    MISSING_REASON,
    BashCallGate,
    CommandTier,
    CommandVerdict,
    classify_command,
)
from ..skills_materialize import materialize_skill_package

EventEmitter = Callable[[str, dict[str, Any]], None]
WaitingCallback = Callable[[dict[str, Any], ToolBatch, int], None]
AgentFactory = Callable[["TraeRuntimeSettings", Any, Path], Any]

logger = logging.getLogger(__name__)


SIDE_EFFECT_TOOLS = {
    "bash",
    "json_edit_tool",
    "str_replace_based_edit_tool",
    "word_edit_tool",
    "excel_edit_tool",
    "pdf_tool",
    "document_convert_tool",
}

DOCUMENT_TOOL_NAMES = {
    "word_edit_tool",
    "excel_edit_tool",
    "pdf_tool",
    "document_convert_tool",
}

ASK_USER_TOOL = "ask_user"

#: 生成会话在 ask_user 被确认前只允许这些只读动作。
PRE_ASK_READ_COMMANDS = frozenset({"view", "read", "show", "list", "get"})

#: ask_user 确认前会被拦截的动作（写文件工具 + 结束标记）。
PRE_ASK_BLOCKED_TOOLS = (
    "bash",
    "str_replace_based_edit_tool",
    "json_edit_tool",
    "word_edit_tool",
    "excel_edit_tool",
    "pdf_tool",
    "document_convert_tool",
    "task_done",
)

_QUICK_TALK_HINTS = (
    "你好",
    "你是谁",
    "你叫什么",
    "介绍下你自己",
    "介绍一下你自己",
    "你能做什么",
    "你会做什么",
    "hello",
    "hi",
    "hey",
    "谢谢",
    "感谢",
    "再见",
    "bye",
    "在吗",
)


def _is_quick_talk(task: str) -> bool:
    """Whether a task looks like greeting / quick Q&A rather than a job request."""

    text = (task or "").strip()
    if not text or len(text) > 120:
        return False
    lowered = text.lower()
    if any(hint in lowered for hint in _QUICK_TALK_HINTS):
        return True
    # Short Chinese/English question sentences (no file/tool keywords).
    if len(text) <= 60 and ("？" in text or "?" in text or "吗" in text or "呢" in text):
        if not any(k in lowered for k in ("文件", "工具", "workspace", "创建", "修复", "写", "test", "修复")):
            return True
    return False


def _looks_like_meta_apology(text: str) -> bool:
    """Detect a model turn that only apologizes about the previous turn's ending."""

    value = (text or "").strip()
    if not value or len(value) > 180:
        return False
    return (
        "没有正确收尾" in value
        or ("抱歉" in value and "收尾" in value)
        or ("抱歉" in value and "刚才" in value and "任务" in value)
    )


def _interaction_text(response: Any) -> str:
    """Extract the assistant text of one LLM response (string or blocks)."""

    content = (response or {}).get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts).strip()
    return ""


def _first_real_assistant_text(payload: dict[str, Any]) -> str:
    """Return the first substantive assistant text that is not a meta apology."""

    for interaction in payload.get("llm_interactions") or []:
        text = _interaction_text(interaction.get("response") or {})
        if text and not _looks_like_meta_apology(text):
            return text
    return ""


def _split_env(value: str) -> tuple[str, ...]:
    """Comma-separated env var -> tuple (empty entries dropped)."""

    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


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
    #: Run-scoped skill materialization root: ``<skills_root>/<run_id>``.
    skills_root: Path = Path("/opt/agent-skills")
    #: Run-scoped scratch space: ``<run_tmp_root>/<run_id>``. It is created for
    #: the run and exposed to the command gate as an allowed path prefix so a
    #: CLI can use it for temporary files without leaving the sandbox.
    run_tmp_root: Path = Path(tempfile.gettempdir()) / "agentsupport-runs"
    #: ``llm`` (default) | ``deny`` | ``off`` for risky commands in silent mode.
    command_approval_mode: str = "llm"
    command_approval_timeout_seconds: float = 20.0
    judge_provider: str | None = None
    judge_model: str | None = None
    judge_base_url: str | None = None
    judge_api_key: str | None = None
    #: Deployment can only tighten the command lists, never widen them.
    denied_commands: tuple[str, ...] = ()
    disabled_safe_commands: tuple[str, ...] = ()

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
        judge_provider = os.getenv("SESSION_RUNNER_JUDGE_PROVIDER") or provider
        judge_model = os.getenv("SESSION_RUNNER_JUDGE_MODEL") or os.getenv(
            "TRAE_MODEL", "claude-sonnet-4-20250514"
        )
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
            skills_root=Path(
                os.getenv("SESSION_RUNNER_SKILLS_ROOT", "/opt/agent-skills")
            ),
            run_tmp_root=Path(
                os.getenv(
                    "SESSION_RUNNER_RUN_TMP_ROOT",
                    str(Path(tempfile.gettempdir()) / "agentsupport-runs"),
                )
            ),
            command_approval_mode=os.getenv("SESSION_RUNNER_COMMAND_APPROVAL", "llm"),
            command_approval_timeout_seconds=float(
                os.getenv("SESSION_RUNNER_COMMAND_APPROVAL_TIMEOUT_SECONDS", "20")
            ),
            judge_provider=judge_provider,
            judge_model=judge_model,
            judge_base_url=os.getenv("SESSION_RUNNER_JUDGE_MODEL_BASE_URL")
            or os.getenv("TRAE_MODEL_BASE_URL")
            or None,
            judge_api_key=os.getenv("SESSION_RUNNER_JUDGE_API_KEY") or api_key,
            denied_commands=_split_env(os.getenv("SESSION_RUNNER_DENIED_COMMANDS", "")),
            disabled_safe_commands=_split_env(
                os.getenv("SESSION_RUNNER_DISABLED_SAFE_COMMANDS", "")
            ),
        )

    def skill_root(self, run_id: str) -> Path:
        return self.skills_root / run_id

    def command_policy(self):
        from session_runner.security import DEFAULT_POLICY

        return DEFAULT_POLICY.tightened(
            disable_safe=self.disabled_safe_commands, deny_more=self.denied_commands
        )

    def judge_settings(self):
        from session_runner.security.judge import JudgeSettings

        return JudgeSettings(
            config_path=self.config_path,
            provider=self.judge_provider or self.provider,
            model=self.judge_model or self.model,
            model_base_url=self.judge_base_url or self.model_base_url,
            api_key=self.judge_api_key or self.api_key,
            timeout_seconds=self.command_approval_timeout_seconds,
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


def _bundle_get(bundle: Any, key: str, default: Any = None) -> Any:
    """Read one field from a request context bundle (dict or model)."""

    if isinstance(bundle, dict):
        return bundle.get(key, default)
    return getattr(bundle, key, default)


def install_retry_delta_guard(client: Any, emit: EventEmitter) -> bool:
    """Emit ``message.reset`` when a retried model call restarts its stream.

    ``retry_with`` re-runs the whole call after a read timeout, and every delta
    of every attempt is forwarded. Without this guard the discarded attempt's
    text stays in the stream, so a viewer sees the answer cut off mid-sentence
    and then repeated from the start.

    The attempt counter is reset per ``chat()`` call, so a normal next-step call
    is not mistaken for a retry. Returns ``True`` when the guard was installed;
    clients without the Anthropic streaming hook (openai-compatible providers)
    are left untouched.
    """

    if getattr(client, "_agentsupport_retry_guard", False):
        return False
    original_chat = getattr(client, "chat", None)
    original_stream = getattr(client, "_create_anthropic_response_stream", None)
    if original_chat is None or original_stream is None:
        return False
    attempts = {"count": 0}

    def chat(*args: Any, **kwargs: Any) -> Any:
        attempts["count"] = 0
        return original_chat(*args, **kwargs)

    def stream(*args: Any, **kwargs: Any) -> Any:
        attempts["count"] += 1
        if attempts["count"] > 1:
            emit(
                "message.reset",
                {"reason": "stream_retry", "attempt": attempts["count"]},
            )
        return original_stream(*args, **kwargs)

    client.chat = chat
    client._create_anthropic_response_stream = stream
    client._agentsupport_retry_guard = True
    return True


def _context_catalog(bundle: Any) -> list[dict[str, Any]]:
    """Extract the candidate-pool catalog from a request context bundle."""

    entries = _bundle_get(bundle, "skill_catalog") or []
    return [entry for entry in entries if isinstance(entry, dict)]


def _catalog_listing(entries: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for entry in entries:
        skill_id = str(entry.get("skill_id") or "unknown")
        description = str(entry.get("description") or "").strip()
        name = str(entry.get("name") or "").strip()
        label = f"{skill_id}（{name}）" if name and name != skill_id else skill_id
        lines.append(f"- {label}: {description}" if description else f"- {label}")
    return "\n".join(lines)


def _skill_prompt_section(bundle: Any, skill_root: str | None) -> str | None:
    """Build the catalog section: what is available, and how to read it."""

    entries = _context_catalog(bundle)
    if not entries or not skill_root:
        return None
    return SKILL_CATALOG_PROMPT.format(
        root=skill_root, catalog=_catalog_listing(entries)
    )


SKILL_CATALOG_PROMPT = """# 可用 Skills

本次会话可用的 Skill 目录（只给名称与用途）。读取根目录：{root}

规则：
1. 任务与某个 Skill 的描述匹配时，**必须先读取它的 SKILL.md，再判断是否采用**。
   不要求你一定采用它，但作出判断前必须确保信息充足；除非用户明确要求，否则不要跳过读取。
2. 读取用 bash，例如：`cat {root}/<skill_id>/SKILL.md`。每条命令都要带 reason，
   且只有只读命令、路径在该根目录或当前会话工作区内才会被放行。
3. SKILL.md 较大时先用 `grep` 定位需要的部分，不要整篇读进来。

目录：
{catalog}"""


FILE_REFERENCE_PROMPT = """# 文件引用格式
当你在回复中列出或提及工作区内的文件时，每个文件用以下格式（不要用反引号包裹）：
[[file:绝对路径|显示名]]
- 绝对路径是文件在磁盘上的真实绝对路径（以工具返回的实际路径为准），例如 {example}
- 显示名可省略：[[file:绝对路径]]"""


def _file_reference_section(bundle: Any) -> str | None:
    """Core (non-skill) file-reference-format instruction, gated by flag."""
    if isinstance(bundle, dict):
        flag = bool(bundle.get("file_ref_format"))
        workspace_ref = bundle.get("workspace_ref")
    else:
        flag = bool(getattr(bundle, "file_ref_format", False))
        workspace_ref = getattr(bundle, "workspace_ref", None)
    if not flag:
        return None
    if workspace_ref:
        base = str(workspace_ref).rstrip("/\\")
        example = f"[[file:{base}/report.docx|report.docx]]"
    else:
        example = "[[file:/workspace-data/<会话工作区>/report.docx|report.docx]]"
    return FILE_REFERENCE_PROMPT.format(example=example)


def _default_agent_factory(settings: TraeRuntimeSettings, request: Any, trajectory: Path) -> Any:
    _ensure_vendored_trae_path()
    from session_runner.tools.bash_reason_tool import register_bash_tool
    from session_runner.tools.document_tools import register_document_tools
    from session_runner.tools.question_tool import register_question_tool

    register_bash_tool()
    register_document_tools()
    register_question_tool()
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


def prepare_run_tmp(settings: TraeRuntimeSettings, run_id: str) -> Path | None:
    """Create the run-scoped scratch directory ``<run_tmp_root>/<run_id>``."""

    if not run_id:
        return None
    root = Path(settings.run_tmp_root).resolve() / run_id
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover - filesystem dependent
        logger.warning("failed to create run tmp dir %s: %s", root, exc)
        return None
    return root


def cleanup_run_tmp(path: Path | None) -> None:
    """Remove a run-scoped scratch directory (best effort, never raises)."""

    if path is None:
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError as exc:  # pragma: no cover - filesystem dependent
        logger.warning("failed to clean up run tmp dir %s: %s", path, exc)


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


def _usage_payload(usage: Any) -> dict[str, int] | None:
    """Normalize a Trae ``LLMUsage`` into the run event usage contract."""

    if usage is None:
        return None
    return {
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_creation_input_tokens": int(
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        ),
        "cache_read_input_tokens": int(
            getattr(usage, "cache_read_input_tokens", 0) or 0
        ),
        "reasoning_tokens": int(getattr(usage, "reasoning_tokens", 0) or 0),
    }


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
        workspace_root: Path | None = None,
        bash_gate: BashCallGate | None = None,
        run_id: str = "",
        conversation_id: str = "",
    ) -> None:
        self.delegate = delegate
        self.emit = emit
        self.on_waiting = on_waiting
        self.next_step = next_step
        self.mode = str(raw_policy.get("mode") or "default")
        self.workspace_root = workspace_root
        self.bash_gate = bash_gate
        self.run_id = run_id
        self.conversation_id = conversation_id
        configured = {
            item["name"]: ToolDescriptor.model_validate(item)
            for item in raw_policy.get("tools", [])
        }
        if self.mode == "no_approval":
            # 无审批模式：全部工具可用（含 bash），无工作区限制。
            allowed = set(tool_names)
            approvals: set[str] = set()
            force_no_approval = True
        elif self.mode == "silent":
            # 静默模式：仅工作区白名单工具、路径受限、无审批。
            allowed = set(raw_policy.get("allowed_tools", [])) & set(tool_names)
            approvals = set()
            force_no_approval = True
        else:
            allowed = set(raw_policy.get("allowed_tools", tool_names))
            approvals = set(
                raw_policy.get(
                    "approval_required_tools",
                    SIDE_EFFECT_TOOLS.intersection(allowed),
                )
            )
            force_no_approval = False
        descriptors = [
            configured.get(
                name,
                ToolDescriptor(
                    name=name,
                    requires_approval=(
                        (not force_no_approval)
                        and (name in SIDE_EFFECT_TOOLS or name.startswith("mcp."))
                    ),
                ),
            )
            for name in tool_names
        ]
        self.control_plane = ToolGatewayControlPlane(descriptors)
        # In default mode the command gate owns bash approval: a batch whose bash
        # calls are all whitelist-safe drops ``bash`` from the approval set for
        # that batch only, so a read does not need a human click. The tool-level
        # flag stays on the descriptor path for every other mode.
        if self.mode == "default":
            for descriptor in descriptors:
                if descriptor.name == "bash":
                    descriptor.requires_approval = False
            approvals.add("bash")
        self.policy = ToolGatewayPolicy(
            allowed_tools=allowed,
            approval_required_tools=approvals,
        )
        self.pending_batch: ToolBatch | None = None
        self.pending_authorization: Any = None
        self.pending_calls: list[Any] = []
        self.pending_method: str | None = None
        self._decision: asyncio.Future[ApprovalDecision] | None = None
        self._answer: asyncio.Future[str] | None = None
        self._results: dict[str, Any] = {}
        self.ask_gate = ASK_USER_TOOL in self.policy.allowed_tools
        self.ask_answered = False

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

    def submit_answer(self, answer: str) -> None:
        if self._answer is None or self._answer.done():
            raise RuntimeError("Trae run is not waiting for a user question")
        self._answer.set_result(str(answer))

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

        blocked, all_bash_safe = self._preflight(calls)
        if blocked and len(blocked) == len(calls):
            # A batch that is only forbidden commands never reaches the human gate.
            return self._store_results(
                [
                    self._rejection(
                        call, f"命令被拒绝（不可用清单）：{blocked[call.call_id]}"
                    )
                    for call in calls
                ]
            )

        authorization = self.control_plane.authorize(
            batch,
            self._effective_policy(all_bash_safe, restored_decision=restored_decision),
        )
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
        if any(call.name == ASK_USER_TOOL for call in batch.calls):
            return await self._ask_user(batch, calls, method)
        if self.ask_gate and not self.ask_answered:
            blocked = [
                call
                for call in batch.calls
                if self._blocked_before_ask(call)
            ]
            if blocked:
                names = ", ".join(sorted({call.name for call in blocked}))
                return self._rejected(
                    calls,
                    "写文件/结束动作被拦截：请先单独调用 ask_user 把准备总结的业务场景、"
                    "要固化的口径/规范/方法与边界发给用户确认；用户回复后才能继续。已拦截："
                    + names,
                )
        return await self._dispatch(calls, method)

    async def _dispatch(self, calls: list[Any], method: str) -> list[Any]:
        """Apply the silent sandbox and the command gate, then run the survivors."""

        blocked: dict[str, str] = {}
        for call in calls:
            problem = await self._call_problem(call)
            if problem is not None:
                blocked[call.call_id] = problem

        executed: dict[str, Any] = {}
        remaining = [call for call in calls if call.call_id not in blocked]
        if remaining:
            for result in await getattr(self.delegate, method)(remaining):
                executed[result.call_id] = result

        ordered: list[Any] = []
        for call in calls:
            problem = blocked.get(call.call_id)
            ordered.append(self._rejection(call, problem) if problem else executed[call.call_id])
        return self._store_results(ordered)

    def _bash_verdict(self, call: Any) -> CommandVerdict | None:
        """Deterministic tier for one bash call; ``None`` for other tools."""

        if call.name != "bash" or self.bash_gate is None:
            return None
        arguments = call.arguments if isinstance(call.arguments, dict) else {}
        if not str(arguments.get("reason") or "").strip():
            return CommandVerdict(CommandTier.BLOCKED, MISSING_REASON)
        return classify_command(
            str(arguments.get("command") or ""),
            allowed_prefixes=self.bash_gate.allowed_prefixes,
            cwd=self.bash_gate.cwd,
            policy=self.bash_gate.policy,
        )

    def _preflight(self, calls: list[Any]) -> tuple[dict[str, str], bool]:
        """Return (blocked bash reasons, whether every bash call is whitelist-safe)."""

        blocked: dict[str, str] = {}
        bash_calls = 0
        safe_calls = 0
        for call in calls:
            verdict = self._bash_verdict(call)
            if verdict is None:
                continue
            bash_calls += 1
            if verdict.tier is CommandTier.BLOCKED:
                blocked[call.call_id] = verdict.reason
            elif verdict.tier is CommandTier.SAFE:
                safe_calls += 1
        return blocked, bash_calls > 0 and safe_calls == bash_calls

    def _effective_policy(
        self, all_bash_safe: bool, *, restored_decision: ApprovalDecision | None
    ) -> ToolGatewayPolicy:
        """Drop ``bash`` from this batch's approval set when the gate already cleared it."""

        if self.mode != "default" or not all_bash_safe or restored_decision is not None:
            return self.policy
        return ToolGatewayPolicy(
            allowed_tools=self.policy.allowed_tools,
            approval_required_tools=self.policy.approval_required_tools - {"bash"},
        )

    async def _call_problem(self, call: Any) -> str | None:
        """Return a rejection reason for one call, or ``None`` to let it run."""

        if self.mode == "silent":
            problem = self._sandbox_problem(call)
            if problem is not None:
                self._warn_sandbox(call, problem)
                return f"silent mode sandbox: {problem}"

        if call.name != "bash" or self.bash_gate is None:
            return None

        arguments = call.arguments if isinstance(call.arguments, dict) else {}
        outcome = await self.bash_gate.check(
            command=str(arguments.get("command") or ""),
            reason=str(arguments.get("reason") or ""),
            run_id=self.run_id,
            conversation_id=self.conversation_id,
        )
        self.emit(
            "command.gate",
            {
                "call_id": call.call_id,
                "tier": outcome.tier.value if outcome.tier else None,
                "allowed": outcome.allowed,
                "source": outcome.source,
                "reason": outcome.reason,
            },
        )
        if outcome.allowed:
            return None
        return f"命令被拒绝（{outcome.source}）：{outcome.reason}"

    def _rejection(self, call: Any, reason: str) -> Any:
        return _trae_tool_result(
            call_id=call.call_id,
            tool_id=getattr(call, "id", None),
            name=call.name,
            success=False,
            error=reason,
        )

    def _blocked_before_ask(self, call: Any) -> bool:
        """Whether a call must wait until the user answered the ask_user gate."""

        name = call.name
        if name not in PRE_ASK_BLOCKED_TOOLS:
            return False
        if name == "task_done":
            return True
        arguments = call.arguments if isinstance(call.arguments, dict) else {}
        command = str(arguments.get("command") or "")
        return command not in PRE_ASK_READ_COMMANDS

    async def _ask_user(
        self,
        batch: ToolBatch,
        calls: list[Any],
        method: str,
    ) -> list[Any]:
        """Pause at a durable human gate and return the user's answer as the tool result."""

        if len(batch.calls) != 1:
            raise RuntimeError(
                "ask_user must be called alone; retry with a single ask_user call"
            )
        call = batch.calls[0]
        arguments = dict(call.arguments or {})
        question = str(arguments.get("question") or "").strip()
        if not question:
            return self._store_results(
                [
                    _trae_tool_result(
                        call_id=call.call_id,
                        tool_id=getattr(call, "id", None),
                        name=call.name,
                        success=False,
                        error=(
                            "ask_user 缺少 question 参数：请用中文完整列出你准备总结的"
                            "业务场景、要固化的口径/规范/方法与边界后重试，不要省略该参数。"
                        ),
                    )
                ]
            )
        interaction_id = uuid4().hex
        self.pending_batch = batch
        self.pending_calls = calls
        self.pending_method = method
        self._answer = asyncio.get_running_loop().create_future()
        interaction = {
            "interaction_id": interaction_id,
            "kind": "question",
            "question": question,
            "tool_batch_hash": batch.batch_hash,
            "tool_batch": batch.model_dump(mode="json"),
            "pending_tool_calls": [
                {
                    "call_id": getattr(item, "call_id", None),
                    "name": getattr(item, "name", None),
                    "arguments": dict(getattr(item, "arguments", {}) or {}),
                }
                for item in calls
            ],
            "tool_policy": {
                "allowed_tools": sorted(self.policy.allowed_tools),
                "approval_required_tools": sorted(self.policy.approval_required_tools),
                "tools": [
                    item.model_dump(mode="json")
                    for item in self.control_plane.describe_tools()
                ],
            },
            "tool_method": method,
            "next_step": self.next_step(),
        }
        self.emit("interaction.requested", interaction)
        self.on_waiting(interaction, batch, interaction["next_step"])
        answer = await self._answer
        self.ask_answered = True
        result = _trae_tool_result(
            call_id=call.call_id,
            tool_id=getattr(call, "id", None),
            name=call.name,
            success=True,
            result=f"用户回复：{answer}",
        )
        return self._store_results([result])

    def _store_results(self, results: list[Any]) -> list[Any]:
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

    def _sandbox_problem(self, call: Any) -> str | None:
        """静默模式下校验工作区边界；越界返回原因，未越界返回 None。"""

        if call.name not in DOCUMENT_TOOL_NAMES | {"str_replace_based_edit_tool", "json_edit_tool"}:
            return None
        arguments = call.arguments if isinstance(call.arguments, dict) else {}
        path_keys = (
            ("input_path", "output_path")
            if call.name == "document_convert_tool"
            else ("path",)
        )
        if self.workspace_root is None:
            return None
        root = self.workspace_root.resolve()
        for key in path_keys:
            path = arguments.get(key)
            if not isinstance(path, str) or not path.strip():
                continue
            try:
                candidate = Path(path)
                if not candidate.is_absolute():
                    candidate = self.workspace_root / candidate
                resolved = candidate.resolve()
            except OSError as exc:
                return f"invalid path {path!r}: {exc}"
            if not (resolved == root or resolved.is_relative_to(root)):
                return f"path outside workspace: {path}"
        return None

    def _warn_sandbox(self, call: Any, problem: str) -> None:
        message = f"silent mode sandbox rejected tool {call.name}: {problem}"
        logger.warning(message)
        self.emit(
            "run.warning",
            {
                "code": "SANDBOX_REJECTED",
                "kind": "sandbox",
                "message": message,
            },
        )

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
        self._answer = None


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
        from ..security.judge import build_command_approver

        self.command_approver = build_command_approver(
            self.settings.command_approval_mode,
            self.settings.judge_settings(),
        )
        self.workspace: Path | None = None
        self.trajectory: Path | None = None
        self.skill_root: Path | None = None
        self.tmp_dir: Path | None = None
        self.agent: Any = None
        self.bridge: TraeToolGatewayBridge | None = None
        self.next_step = 1

    def approve(self, decision: ApprovalDecision) -> None:
        if self.bridge is None:
            raise RuntimeError("Trae tool gateway is not initialized")
        self.bridge.approve(decision)

    def submit_answer(self, answer: str) -> None:
        if self.bridge is None:
            raise RuntimeError("Trae tool gateway is not initialized")
        self.bridge.submit_answer(answer)

    def _bash_gate(self) -> BashCallGate:
        """Gate bash calls against the session workspace, skill root and run tmp.

        Tenant presets may declare sub-command prefixes that are safe to run
        without approval; those arrive in ``context_bundle.cli_policy``.
        """

        run_id = str(getattr(self.request, "run_id", "") or "")
        policy = getattr(self.request, "tool_policy", None) or {}
        skill_root = self.skill_root or self.settings.skill_root(run_id)
        allowed = [str(self.workspace), str(skill_root)]
        if self.tmp_dir is not None:
            allowed.append(str(self.tmp_dir))
        return BashCallGate(
            approver=self.command_approver,
            allowed_prefixes=tuple(allowed),
            cwd=str(self.workspace),
            mode=str(policy.get("mode") or "default"),
            policy=self.settings.command_policy(),
            declared_safe_prefixes=tuple(self._cli_policy().get("allowed_safe_prefixes") or ()),
        )

    def _cli_policy(self) -> dict[str, Any]:
        """The tenant preset's runtime CLI policy, if the platform sent one."""

        bundle = getattr(self.request, "context_bundle", None)
        policy = _bundle_get(bundle, "cli_policy") if bundle is not None else None
        return dict(policy) if isinstance(policy, dict) else {}

    def checkpoint_policy(self) -> dict[str, Any]:
        return {
            "next_step": self.next_step,
            "trajectory_ref": str(self.trajectory) if self.trajectory else None,
        }

    async def run(self) -> dict[str, Any]:
        await self._initialize_agent()
        task = self.request.context_bundle.get("task", "")
        self.agent.agent.new_task(task, {"project_path": str(self.workspace)})
        history = self._session_history_messages()
        if history:
            # A conversation is one exchange; a session is the collection of
            # many exchanges. Insert earlier rounds' dialogue in front of the
            # current task message so the agent sees the full session history.
            self.agent.agent._initial_messages[1:1] = history
        if _is_quick_talk(task):
            # Greeting / quick Q&A: the agent may answer without calling
            # task_done in the same turn. Allow a pure-text reply to complete
            # so it is not pushed into a second "meta apology" turn.
            setattr(self.agent.agent, "complete_on_text_only", True)
        execution = await self.agent.agent.execute_task()
        self._emit_trajectory()
        if not execution.success:
            step_error = execution.steps[-1].error if execution.steps else None
            raise RuntimeError(step_error or execution.final_result or "Trae execution failed")
        content = execution.final_result or ""
        degraded = _looks_like_meta_apology(content)
        if not content or degraded:
            content = self._fallback_final_content(prefer_first=degraded)
            if content:
                self._warn_content_degradation(
                    "fallback",
                    "final result was empty/degraded; recovered an earlier assistant text",
                )
            else:
                self._warn_content_degradation(
                    "empty",
                    "final result is empty/degraded and no fallback text is available",
                )
        result = {
            "status": "completed",
            "content": content,
            "steps": len(execution.steps),
        }
        usage = _usage_payload(getattr(execution, "total_tokens", None))
        if usage is not None:
            result["usage"] = usage
        if content:
            self.emit("message", {"content": content})
        return result

    def _fallback_final_content(self, *, prefer_first: bool = False) -> str:
        """Recover text the model produced before a content-less task_done.

        Trae's completion check only looks for a ``task_done`` tool call; a
        model that answers in one turn without ``task_done`` is pushed into
        another turn where it may call ``task_done`` with empty content,
        discarding the real answer. Fall back to the last non-empty
        assistant text recorded in the trajectory.
        """
        if not self.trajectory or not self.trajectory.is_file():
            return ""
        try:
            payload = json.loads(self.trajectory.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return ""
        if prefer_first:
            return _first_real_assistant_text(payload)
        for interaction in reversed(payload.get("llm_interactions") or []):
            text = _interaction_text(interaction.get("response") or {})
            if text:
                return text
        return ""

    def _warn_content_degradation(self, kind: str, detail: str) -> None:
        """Log and emit a prominent warning whenever content is degraded."""

        message = f"run content degradation ({kind}): {detail}"
        logger.warning(
            "run_id=%s conversation_id=%s %s",
            self.request.run_id,
            self.request.conversation_id,
            message,
        )
        self.emit(
            "run.warning",
            {
                "code": "CONTENT_DEGRADATION",
                "kind": kind,
                "message": message,
            },
        )

    def _session_history_messages(self) -> list[Any]:
        """Turn prior session dialogue into LLM messages for this run.

        The control plane aggregates earlier conversations' instructions and
        assistant replies into ``context_bundle.recent_events``; without this,
        the agent would only ever see the current task.

        **失败/中止的那一轮也要进上下文**（2026-09-18）：失败时这一轮没有
        ``message`` 事件，只按 ``message`` 过滤的话，模型会以为上一轮根本不存在
        （上下文里直接是两条相邻的 user 任务），于是"再试一次"会重头再来、
        甚至把已经写了一半的文档当成新任务。失败也是信息，这里补一条 assistant
        说明，让模型知道上一轮干到哪、为什么停。
        """
        bundle = getattr(self.request, "context_bundle", None)
        if isinstance(bundle, dict):
            events = bundle.get("recent_events") or []
        else:
            events = getattr(bundle, "recent_events", None) or []
        if not events:
            return []

        _ensure_vendored_trae_path()
        from trae_agent.utils.llm_clients.llm_basics import LLMMessage

        messages: list[Any] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            payload = event.get("payload") or {}
            if kind == "message":
                role = payload.get("role") or "assistant"
                content = payload.get("content")
                if role in ("user", "assistant") and content:
                    messages.append(LLMMessage(role=role, content=str(content)))
                continue
            if kind == "run.failed":
                reason = payload.get("error") or payload.get("message") or "未知原因"
                messages.append(LLMMessage(
                    role="assistant",
                    content=f"[上一轮运行失败] {reason}（这一轮没有完成，文档可能只改了一部分；"
                            f"继续之前先确认文档当前状态，不要假设上一轮没发生过）",
                ))
            elif kind == "run.cancelled":
                messages.append(LLMMessage(
                    role="assistant",
                    content="[上一轮被用户停止] 未完成，文档可能只改了一部分。",
                ))
        return messages

    async def resume(
        self,
        checkpoint: Any,
        value: Any,
        *,
        kind: str = "approval",
    ) -> dict[str, Any]:
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
        if kind == "question":
            answer = str(value)
            tool_results = [
                _trae_tool_result(
                    call_id=call.call_id,
                    tool_id=getattr(call, "id", None),
                    name=call.name,
                    success=True,
                    result=f"用户回复：{answer}",
                )
                for call in calls
            ]
            for tool_result in tool_results:
                self.emit(
                    "tool.result",
                    {
                        "call_id": tool_result.call_id,
                        "name": tool_result.name,
                        "success": True,
                        "output": tool_result.result,
                        "error": None,
                    },
                )
            if self.bridge is not None:
                self.bridge.ask_answered = True
        else:
            tool_results = await self.bridge.execute_restored(
                calls, method, ApprovalDecision(str(value))
            )
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
        content = execution.final_result or ""
        if not content:
            content = self._fallback_final_content()
            if content:
                self._warn_content_degradation(
                    "fallback",
                    "resume final result was empty; recovered the last non-empty assistant text",
                )
            else:
                self._warn_content_degradation(
                    "empty",
                    "resume final result is empty and no fallback text is available",
                )
        if content:
            self.emit("message", {"content": content})
        result = {
            "status": "completed",
            "content": content,
            "steps": len(execution.steps),
            "resumed": True,
        }
        usage = _usage_payload(getattr(execution, "total_tokens", None))
        if usage is not None:
            result["usage"] = usage
        return result

    def _mcp_configs(self) -> dict[str, Any]:
        """把两条来源的 MCP 配置统一成 ``MCPServerConfig``。

        两条来源：
          1. runner 静态配置（``trae_config.yaml`` 的 ``mcp_servers``，本来就是对象）；
          2. **每轮下发**的 ``mcp_servers_config``（来自会话/对话的 ``mcp_refs``，
             到这里还是 dict）。

        为什么必须转换：vendored 客户端对配置做的是**属性访问**
        （``mcp_server_config.http_url`` / ``command`` …），dict 会直接
        ``AttributeError``，而 ``discover_mcp_tools`` 用 ``except Exception: continue``
        把它吞掉——表现就是"模型看不到任何 MCP 工具"且日志无痕。
        """

        from trae_agent.utils.config import MCPServerConfig

        merged: dict[str, Any] = {}
        existing = getattr(self.agent.agent, "mcp_servers_config", None) or {}
        for name, value in existing.items():
            merged[name] = value if isinstance(value, MCPServerConfig) else MCPServerConfig(**value)
        for name, value in (self.mcp_servers_config or {}).items():
            merged[name] = value if isinstance(value, MCPServerConfig) else MCPServerConfig(**value)
        return merged

    async def _initialize_agent(self) -> None:
        self.settings.validate()
        self.workspace = self.settings.workspace(self.request.workspace_ref)
        trajectory_dir = self.workspace / ".agentsupport" / "trajectories"
        trajectory_dir.mkdir(parents=True, exist_ok=True)
        self.trajectory = trajectory_dir / f"{self.request.run_id}.json"
        run_id = str(getattr(self.request, "run_id", "") or "")
        self.skill_root = materialize_skill_package(
            _bundle_get(self.request.context_bundle, "skill_package") or [],
            self.settings.skill_root(run_id),
        )
        self.tmp_dir = prepare_run_tmp(self.settings, run_id)
        self.agent = self.agent_factory(self.settings, self.request, self.trajectory)
        sections = []
        skill_section = _skill_prompt_section(
            self.request.context_bundle,
            str(self.skill_root) if self.skill_root else None,
        )
        if skill_section:
            sections.append(skill_section)
        ref_section = _file_reference_section(self.request.context_bundle)
        if ref_section:
            sections.append(ref_section)
        trae_agent = getattr(self.agent, "agent", None)
        if sections and trae_agent is not None:
            getter = getattr(trae_agent, "get_system_prompt", None)
            current = getter() if callable(getter) else getattr(trae_agent, "_system_prompt", None)
            extra = "\n\n".join(sections)
            trae_agent._system_prompt = f"{current or ''}\n\n{extra}".strip()
        mcp_configs = self._mcp_configs()
        if mcp_configs:
            self.agent.agent.mcp_servers_config = mcp_configs
            self.agent.agent.allow_mcp_servers = list(mcp_configs)
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
            workspace_root=self.workspace,
            bash_gate=self._bash_gate(),
            run_id=str(getattr(self.request, "run_id", "") or ""),
            conversation_id=str(getattr(self.request, "conversation_id", "") or ""),
        )
        logger.info(
            "bridge ready: ask_tool_available=%s ask_gate=%s allowed=%s",
            ASK_USER_TOOL in tool_names,
            self.bridge.ask_gate,
            sorted(self.bridge.policy.allowed_tools),
        )
        mcp_names = {name for name in tool_names if name.startswith("mcp.")}
        if mcp_names:
            self.bridge.policy.allowed_tools.update(mcp_names)
        self.agent.agent._tool_caller = self.bridge
        llm_client = getattr(trae_agent, "_llm_client", None)
        if llm_client is not None:
            inner = getattr(llm_client, "client", llm_client)
            if getattr(inner, "on_text_delta", None) is None:
                inner.on_text_delta = self._emit_message_delta
            if install_retry_delta_guard(inner, self.emit):
                logger.info("retry delta guard installed: a retried stream resets instead of appending")

    def _emit_message_delta(self, delta: str) -> None:
        """Forward one streamed text delta into the runner's in-memory event stream."""
        if not delta:
            return
        self.emit("message.delta", {"delta": delta})

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

"""Command approval behind the deterministic gate.

The gate sorts bash calls into three tiers; only ``RISKY`` reaches an approver.
Everything here is fail-closed: a missing, unparsable or failed judge verdict
denies the command.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .command_gate import CommandTier

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是 AgentSupport 的命令审批器。
Agent 在自己的容器里执行 shell 命令，但只被允许读取两类位置：本次运行的 skill 目录、当前会话工作区。
你要判断一条已经落在"有风险"档的命令是否应当放行。

判定原则：
- 只读、可解释、与 agent 给出的理由一致 → 放行。
- 写文件、改权限、装软件、起进程、访问网络、读取允许范围之外的数据、隐藏真实意图、
  绕过限制（编码、别名、间接执行、探测环境） → 拒绝。
- 证据不足或命令意图说不清 → 拒绝。

只输出 JSON：{"allow": true|false, "rationale": "一句话中文理由"}"""


@dataclass(frozen=True)
class CommandApprovalRequest:
    command: str
    reason: str
    tier: CommandTier
    gate_reason: str
    mode: str
    cwd: str
    allowed_prefixes: tuple[str, ...]
    run_id: str = ""
    conversation_id: str = ""

    @property
    def cache_key(self) -> str:
        payload = json.dumps(
            {
                "command": self.command,
                "reason": self.reason,
                "mode": self.mode,
                "cwd": self.cwd,
                "allowed_prefixes": list(self.allowed_prefixes),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_prompt(self) -> str:
        prefixes = "\n".join(f"- {prefix}" for prefix in self.allowed_prefixes)
        return (
            f"限制要求：\n- 会话模式：{self.mode}\n- 工作目录：{self.cwd}\n"
            f"- 允许读取的前缀：\n{prefixes}\n"
            f"- 确定性判定结论：{self.tier.value}（{self.gate_reason}）\n\n"
            f"Agent 调用的命令：\n{self.command}\n\n"
            f"Agent 给出的理由：\n{self.reason or '（未提供）'}"
        )


@dataclass(frozen=True)
class CommandApproval:
    allowed: bool
    rationale: str
    source: str


@runtime_checkable
class CommandJudge(Protocol):
    """Raw text completion used by the LLM approver."""

    async def complete(self, *, system: str, user: str) -> str: ...


class CommandApprover(Protocol):
    async def approve(self, request: CommandApprovalRequest) -> CommandApproval: ...


class DenyAllApprover:
    """Default when no approver is configured: fail-closed."""

    async def approve(self, request: CommandApprovalRequest) -> CommandApproval:
        return CommandApproval(False, "no command approver is configured", "deny-all")


class AllowAllApprover:
    """Test/development double. Never wire this into silent mode in production."""

    async def approve(self, request: CommandApprovalRequest) -> CommandApproval:
        return CommandApproval(True, "approver disabled", "allow-all")


class LlmCommandApprover:
    """Ask a judge model to read the restriction, the command and the reason."""

    def __init__(self, judge: CommandJudge, *, timeout_seconds: float = 20.0) -> None:
        self.judge = judge
        self.timeout_seconds = timeout_seconds

    async def approve(self, request: CommandApprovalRequest) -> CommandApproval:
        import asyncio

        try:
            raw = await asyncio.wait_for(
                self.judge.complete(system=SYSTEM_PROMPT, user=request.to_prompt()),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            return CommandApproval(False, "approval timed out", "llm")
        except Exception as exc:  # noqa: BLE001 - any judge failure must deny
            logger.warning("command approval failed: %s", exc)
            return CommandApproval(False, f"approval failed: {type(exc).__name__}", "llm")

        parsed = _parse_verdict(raw)
        if parsed is None:
            return CommandApproval(False, "approval verdict was not parsable", "llm")
        return CommandApproval(parsed[0], parsed[1], "llm")


class CachingApprover:
    """Reuse a verdict inside one run. Cross-run reuse is deliberately absent."""

    def __init__(self, delegate: CommandApprover) -> None:
        self.delegate = delegate
        self._cache: dict[str, CommandApproval] = {}

    async def approve(self, request: CommandApprovalRequest) -> CommandApproval:
        key = request.cache_key
        cached = self._cache.get(key)
        if cached is not None:
            return CommandApproval(cached.allowed, f"{cached.rationale} (cached)", cached.source)
        verdict = await self.delegate.approve(request)
        self._cache[key] = verdict
        return verdict


def _parse_verdict(raw: str) -> tuple[bool, str] | None:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("allow"), bool):
        return None
    rationale = payload.get("rationale")
    return payload["allow"], str(rationale) if rationale else ""

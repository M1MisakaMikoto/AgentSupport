"""Judge client and approver wiring for the runner.

The judge reuses the vendored LLM client stack with its own provider/model
settings, defaulting to the agent's model when none are configured.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .approval import (
    AllowAllApprover,
    CachingApprover,
    CommandApprover,
    DenyAllApprover,
    LlmCommandApprover,
)


@dataclass(frozen=True)
class JudgeSettings:
    config_path: Path
    provider: str
    model: str
    model_base_url: str | None
    api_key: str
    timeout_seconds: float = 20.0


class VendoredCommandJudge:
    """One-shot text completion through the vendored client (streamed call)."""

    def __init__(self, settings: JudgeSettings) -> None:
        self.settings = settings

    async def complete(self, *, system: str, user: str) -> str:
        return await asyncio.to_thread(self._complete_sync, system, user)

    def _complete_sync(self, system: str, user: str) -> str:
        from session_runner.adapters.trae import _ensure_vendored_trae_path

        _ensure_vendored_trae_path()
        from trae_agent.utils.config import Config
        from trae_agent.utils.llm_clients.llm_basics import LLMMessage
        from trae_agent.utils.llm_clients.llm_client import LLMClient

        config = Config.create(
            config_file=str(self.settings.config_path)
        ).resolve_config_values(
            provider=self.settings.provider,
            model=self.settings.model,
            model_base_url=self.settings.model_base_url,
            api_key=self.settings.api_key,
        )
        client = LLMClient(config)
        response: Any = client.client.chat(
            [
                LLMMessage(role="system", content=system),
                LLMMessage(role="user", content=user),
            ],
            config,
            tools=None,
            reuse_history=False,
        )
        return str(getattr(response, "content", "") or "")


def build_command_approver(mode: str, judge_settings: JudgeSettings | None) -> CommandApprover:
    """``off`` / ``deny`` / ``llm`` (default). Cache is always per run."""

    normalized = (mode or "llm").strip().lower()
    if normalized in {"off", "allow", "none"}:
        return AllowAllApprover()
    if normalized in {"deny", "closed"}:
        return DenyAllApprover()
    if judge_settings is None:
        return DenyAllApprover()
    return CachingApprover(
        LlmCommandApprover(
            VendoredCommandJudge(judge_settings),
            timeout_seconds=judge_settings.timeout_seconds,
        )
    )

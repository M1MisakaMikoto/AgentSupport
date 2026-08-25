"""Real-model probe for Trae's injectable tool execution boundary."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
TRAE_SOURCE = Path(r"E:\PythonProject\WorkBranch\.tools\trae-agent-src")
CONFIG_PATH = ROOT.parent / "agent-state-experiment" / "results" / "runtime-config.yaml"
WORKSPACE = ROOT / "workspace"
sys.path.insert(0, str(TRAE_SOURCE))

from trae_agent.agent.agent import Agent  # noqa: E402
from trae_agent.tools.base import ToolCall, ToolResult  # noqa: E402
from trae_agent.utils.config import Config  # noqa: E402


class ApprovalExecutor:
    def __init__(self, delegate: Any, decision: str, marker: Path) -> None:
        self.delegate = delegate
        self.decision = decision
        self.marker = marker
        self.interceptions: list[dict[str, object]] = []

    async def close_tools(self):  # type: ignore[no-untyped-def]
        return await self.delegate.close_tools()

    async def _execute(self, tool_calls: list[ToolCall], method: str) -> list[ToolResult]:
        self.interceptions.append({
            "method": method,
            "marker_existed_before_approval": self.marker.exists(),
            "calls": [
                {"call_id": call.call_id, "name": call.name, "arguments": call.arguments}
                for call in tool_calls
            ],
            "decision": self.decision,
        })
        if self.decision == "REJECT":
            return [
                ToolResult(
                    call_id=call.call_id,
                    id=call.id,
                    name=call.name,
                    success=False,
                    error="tool approval decision: REJECT",
                )
                for call in tool_calls
            ]
        return await getattr(self.delegate, method)(tool_calls)

    async def sequential_tool_call(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        return await self._execute(tool_calls, "sequential_tool_call")

    async def parallel_tool_call(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        return await self._execute(tool_calls, "parallel_tool_call")


def create_agent(max_steps: int, trajectory: Path) -> Agent:
    config = Config.create(config_file=str(CONFIG_PATH)).resolve_config_values(
        provider="openai",
        model="qwen3.6-plus",
        model_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        max_steps=max_steps,
    )
    return Agent("trae_agent", config, str(trajectory))


async def run_case(decision: str, marker_name: str, max_steps: int) -> dict[str, object]:
    marker = WORKSPACE / marker_name
    marker.unlink(missing_ok=True)
    trajectory = ROOT / f"real-trae-approval-{decision.lower()}.json"
    agent = create_agent(max_steps, trajectory)
    executor = ApprovalExecutor(agent.agent._tool_caller, decision, marker)
    agent.agent._tool_caller = executor
    task = (
        f"Use an available file editing or bash tool to create {marker} containing exactly "
        f"REAL-TRAE-{decision}. Verify it if the tool succeeds, then finish."
    )
    execution = await agent.run(
        task,
        {"project_path": str(WORKSPACE), "issue": task},
    )
    return {
        "decision": decision,
        "interceptions": executor.interceptions,
        "marker_exists": marker.exists(),
        "marker_content": marker.read_text(encoding="utf-8") if marker.exists() else None,
        "execution_success": execution.success,
        "final_result": execution.final_result,
        "trajectory": str(trajectory),
    }


async def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is required")
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    approved = await run_case("APPROVE_ONCE", "real-approval-approved.txt", 6)
    rejected = await run_case("REJECT", "real-approval-rejected.txt", 2)

    assert approved["interceptions"], "real model did not request a tool"
    assert approved["interceptions"][0]["marker_existed_before_approval"] is False
    assert approved["marker_content"] == "REAL-TRAE-APPROVE_ONCE"
    assert rejected["interceptions"], "real model did not request a rejected tool"
    assert rejected["marker_exists"] is False
    print(json.dumps({"approved": approved, "rejected": rejected}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())

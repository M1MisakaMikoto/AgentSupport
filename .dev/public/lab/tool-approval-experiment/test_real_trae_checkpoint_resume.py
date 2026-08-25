"""Two-process real Trae probe for a platform-owned safe checkpoint."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
TRAE_SOURCE = Path(r"E:\PythonProject\WorkBranch\.tools\trae-agent-src")
CONFIG_PATH = ROOT.parent / "agent-state-experiment" / "results" / "runtime-config.yaml"
WORKSPACE = ROOT / "workspace"
CHECKPOINT_PATH = ROOT / "real-trae-checkpoint.json"
RESULT_PATH = ROOT / "real-trae-checkpoint-resume-result.json"
TRAJECTORY_PATH = ROOT / "real-trae-checkpoint-resume-trajectory.json"
MARKER = WORKSPACE / "real-checkpoint-resume.txt"
TASK = (
    f"Use an available file editing tool to create {MARKER} containing exactly "
    "REAL-TRAE-CHECKPOINT-RESUME. After the tool succeeds, finish the task."
)
sys.path.insert(0, str(TRAE_SOURCE))

from trae_agent.agent.agent import Agent  # noqa: E402
from trae_agent.agent.agent_basics import (  # noqa: E402
    AgentExecution,
    AgentState,
    AgentStep,
    AgentStepState,
)
from trae_agent.tools.base import ToolCall  # noqa: E402
from trae_agent.utils.config import Config  # noqa: E402
from trae_agent.utils.llm_clients.llm_basics import LLMMessage  # noqa: E402


class PauseRequested(BaseException):
    pass


def stable_hash(value: dict[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def create_agent(max_steps: int) -> Agent:
    config = Config.create(config_file=str(CONFIG_PATH)).resolve_config_values(
        provider="openai",
        model="qwen3.6-plus",
        model_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        max_steps=max_steps,
    )
    return Agent("trae_agent", config, str(TRAJECTORY_PATH))


class PendingExecutor:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.calls: list[ToolCall] = []

    async def close_tools(self):  # type: ignore[no-untyped-def]
        return await self.delegate.close_tools()

    async def _pause(self, calls: list[ToolCall]) -> list[Any]:
        self.calls = calls
        context = {
            "task": TASK,
            "project_path": str(WORKSPACE),
            "next_step": 2,
            "pending_tool_calls": [asdict(call) for call in calls],
        }
        checkpoint = {
            "schema_version": 1,
            "context": context,
            "context_hash": stable_hash(context),
            "marker_existed_before_pause": MARKER.exists(),
        }
        CHECKPOINT_PATH.write_text(json.dumps(checkpoint), encoding="utf-8")
        raise PauseRequested()

    async def sequential_tool_call(self, calls: list[ToolCall]) -> list[Any]:
        return await self._pause(calls)

    async def parallel_tool_call(self, calls: list[ToolCall]) -> list[Any]:
        return await self._pause(calls)


async def pause_phase() -> None:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    MARKER.unlink(missing_ok=True)
    CHECKPOINT_PATH.unlink(missing_ok=True)
    agent = create_agent(6)
    pending = PendingExecutor(agent.agent._tool_caller)
    agent.agent._tool_caller = pending
    try:
        await agent.run(TASK, {"project_path": str(WORKSPACE), "issue": TASK})
    except PauseRequested:
        await pending.close_tools()
    else:
        raise AssertionError("Trae completed without reaching the safe tool checkpoint")
    checkpoint = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    assert checkpoint["marker_existed_before_pause"] is False
    assert checkpoint["context"]["pending_tool_calls"]
    print(json.dumps({"phase": "pause", "checkpoint_created": True, "side_effect": False}))


async def resume_phase() -> None:
    checkpoint = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    context = checkpoint["context"]
    assert checkpoint["context_hash"] == stable_hash(context)
    assert not MARKER.exists()

    agent = create_agent(6)
    agent.agent.new_task(TASK, {"project_path": str(WORKSPACE), "issue": TASK})
    calls = [ToolCall(**call) for call in context["pending_tool_calls"]]
    tool_results = await agent.agent._tool_caller.sequential_tool_call(calls)
    messages = list(agent.agent.initial_messages)
    messages.extend(LLMMessage(role="user", tool_result=result) for result in tool_results)
    execution = AgentExecution(task=TASK, steps=[], agent_state=AgentState.RUNNING)

    for step_number in range(context["next_step"], agent.agent.max_steps + 1):
        step = AgentStep(step_number=step_number, state=AgentStepState.THINKING)
        messages = await agent.agent._run_llm_step(step, messages, execution)
        await agent.agent._finalize_step(step, messages, execution)
        if execution.agent_state == AgentState.COMPLETED:
            break

    await agent.agent._close_tools()
    assert execution.success
    assert MARKER.read_text(encoding="utf-8") == "REAL-TRAE-CHECKPOINT-RESUME"
    result = {
        "phase": "resume",
        "checkpoint_validated": True,
        "new_agent_instance": True,
        "tool_executed_once": True,
        "marker_exact": True,
        "execution_success": execution.success,
        "steps_after_resume": len(execution.steps),
        "final_result": execution.final_result,
    }
    RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("pause", "resume"))
    args = parser.parse_args()
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is required")
    if args.phase == "pause":
        asyncio.run(pause_phase())
    else:
        asyncio.run(resume_phase())


if __name__ == "__main__":
    main()

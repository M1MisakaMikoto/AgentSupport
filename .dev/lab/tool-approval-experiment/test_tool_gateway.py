"""Feasibility probe for intercepting Trae built-in tools before execution.

The probe imports the pinned Trae source from WorkBranch, but never calls an
LLM or performs a real tool side effect. It exercises the same ToolExecutor
boundary used by BaseAgent._tool_call_handler.
"""

from __future__ import annotations

import asyncio
import ast
import importlib.util
import json
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


TRAE_SOURCE = Path(r"E:\PythonProject\WorkBranch\.tools\trae-agent-src")
if not TRAE_SOURCE.is_dir():
    raise SystemExit(f"Trae source not found: {TRAE_SOURCE}")
sys.path.insert(0, str(TRAE_SOURCE))

_BASE_SPEC = importlib.util.spec_from_file_location(
    "trae_tool_base", TRAE_SOURCE / "trae_agent" / "tools" / "base.py"
)
assert _BASE_SPEC and _BASE_SPEC.loader
_BASE_MODULE = importlib.util.module_from_spec(_BASE_SPEC)
sys.modules["trae_tool_base"] = _BASE_MODULE
_BASE_SPEC.loader.exec_module(_BASE_MODULE)

Tool = _BASE_MODULE.Tool
ToolCall = _BASE_MODULE.ToolCall
ToolExecResult = _BASE_MODULE.ToolExecResult
ToolResult = _BASE_MODULE.ToolResult
ToolExecutor = _BASE_MODULE.ToolExecutor
ToolParameter = _BASE_MODULE.ToolParameter

@dataclass
class ApprovalRequest:
    call_id: str
    name: str
    arguments: dict[str, object]
    status: str = "PENDING"


class ApprovalBroker:
    def __init__(self) -> None:
        self.requests: dict[str, ApprovalRequest] = {}
        self.waiters: dict[str, asyncio.Future[str]] = {}

    async def request(self, tool_call: ToolCall) -> str:
        request = ApprovalRequest(tool_call.call_id, tool_call.name, tool_call.arguments)
        self.requests[tool_call.call_id] = request
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[str] = loop.create_future()
        self.waiters[tool_call.call_id] = waiter
        return await waiter

    def decide(self, call_id: str, decision: str) -> None:
        request = self.requests[call_id]
        request.status = decision
        self.waiters[call_id].set_result(decision)

    def checkpoint(self) -> dict[str, object]:
        return {call_id: asdict(request) for call_id, request in self.requests.items()}


class ApprovalExecutor(ToolExecutor):
    """Universal gate that runs before every Trae ToolExecutor call."""

    def __init__(self, tools: list[Tool], broker: ApprovalBroker) -> None:
        super().__init__(tools)
        self.broker = broker

    async def execute_tool_call(self, tool_call: ToolCall):  # type: ignore[no-untyped-def]
        decision = await self.broker.request(tool_call)
        if decision != "APPROVE_ONCE":
            return ToolResult(
                call_id=tool_call.call_id,
                name=tool_call.name,
                success=False,
                error=f"tool approval decision: {decision}",
                id=tool_call.id,
            )
        return await super().execute_tool_call(tool_call)


class MarkerTool(Tool):
    def __init__(self, marker: Path) -> None:
        super().__init__(model_provider="test")
        self.marker = marker

    def get_name(self) -> str:
        return "marker_tool"

    def get_description(self) -> str:
        return "Writes a marker after approval."

    def get_parameters(self) -> list[ToolParameter]:
        return []

    async def execute(self, arguments):  # type: ignore[no-untyped-def]
        self.marker.write_text("executed", encoding="utf-8")
        return ToolExecResult(output="marker written")


async def probe_direct_executor() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="tool-gateway-") as directory:
        marker = Path(directory) / "marker.txt"
        broker = ApprovalBroker()
        executor = ApprovalExecutor([MarkerTool(marker)], broker)
        call = ToolCall(name="marker_tool", call_id="call-approve")

        pending = asyncio.create_task(executor.execute_tool_call(call))
        await asyncio.sleep(0)
        assert not marker.exists(), "tool executed before approval"
        assert broker.requests[call.call_id].status == "PENDING"
        checkpoint = broker.checkpoint()
        broker.decide(call.call_id, "APPROVE_ONCE")
        result = await pending
        assert result.success and marker.read_text(encoding="utf-8") == "executed"

        reject_broker = ApprovalBroker()
        reject_executor = ApprovalExecutor([MarkerTool(Path(directory) / "reject.txt")], reject_broker)
        reject_call = ToolCall(name="marker_tool", call_id="call-reject")
        rejected = asyncio.create_task(reject_executor.execute_tool_call(reject_call))
        await asyncio.sleep(0)
        reject_broker.decide(reject_call.call_id, "REJECT")
        reject_result = await rejected
        assert not reject_result.success

        checkpoint_path = Path(directory) / "approval-checkpoint.json"
        checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
        restored = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        assert restored[call.call_id]["name"] == "marker_tool"

        return {
            "intercepted_before_side_effect": True,
            "approve_once_executes": True,
            "reject_prevents_execution": True,
            "approval_checkpoint_round_trip": True,
            "checkpoint_fields": sorted(restored[call.call_id]),
        }


def probe_agent_boundary() -> dict[str, object]:
    source_path = TRAE_SOURCE / "trae_agent" / "agent" / "base_agent.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name == "_tool_call_handler"
    )
    calls = {
        node.func.attr
        for node in ast.walk(handler)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_tool_caller"
    }
    assert {"parallel_tool_call", "sequential_tool_call"}.issubset(calls)
    return {
        "base_agent_tool_call_handler_uses_tool_caller": True,
        "tool_call_methods": sorted(calls),
        "universal_interception_point": "BaseAgent._tool_caller",
    }


async def main() -> None:
    result = {
        "direct_executor": await probe_direct_executor(),
        "agent_boundary": probe_agent_boundary(),
        "recovery_note": (
            "The checkpoint round-trip proves call metadata can persist; a separate "
            "experiment is required to reconstruct Trae LLM history after killing the process."
        ),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

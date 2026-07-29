from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from agent_runner_contracts.tools import (
    ApprovalDecision,
    AuthorizationResult,
    AuthorizationStatus,
    ToolBatch,
    ToolCall,
    ToolGatewayControlPlane,
    ToolGatewayPolicy,
    ToolResult,
)

ToolHandler = Callable[[dict[str, Any]], Awaitable[Any]]


class ToolGatewayExecutor:
    """Runner-side execution plane with batch approval and call-id deduplication."""

    def __init__(
        self,
        control_plane: ToolGatewayControlPlane,
        policy: ToolGatewayPolicy,
        handlers: dict[str, ToolHandler],
    ) -> None:
        self.control_plane = control_plane
        self.policy = policy
        self.handlers = handlers
        self._results: dict[str, ToolResult] = {}

    def describe_tools(self):
        return self.control_plane.describe_tools()

    def authorize(self, batch: ToolBatch) -> AuthorizationResult:
        return self.control_plane.authorize(batch, self.policy)

    def checkpoint(self, batch: ToolBatch, authorization: AuthorizationResult) -> dict[str, Any]:
        return self.control_plane.checkpoint(batch, authorization)

    def restore(self, checkpoint: dict[str, Any]) -> tuple[ToolBatch, AuthorizationResult]:
        return self.control_plane.restore(checkpoint)

    async def execute(
        self,
        batch: ToolBatch,
        authorization: AuthorizationResult,
        decision: ApprovalDecision | None = None,
    ) -> list[ToolResult]:
        if authorization.batch_hash != batch.batch_hash:
            raise ValueError("tool batch hash mismatch")
        if authorization.status == AuthorizationStatus.DENIED:
            return self._rejected(batch, authorization.reason or "tool authorization denied")
        if authorization.status == AuthorizationStatus.REQUIRES_APPROVAL:
            if decision is None:
                raise RuntimeError("tool batch requires approval before execution")
            if decision == ApprovalDecision.REJECT:
                return self._rejected(batch, "tool batch rejected by user")
        results: list[ToolResult] = []
        for call in batch.calls:
            cached = self._results.get(call.call_id)
            if cached:
                results.append(cached)
                continue
            result = await self._execute_call(call)
            self._results[call.call_id] = result
            results.append(result)
        return results

    async def _execute_call(self, call: ToolCall) -> ToolResult:
        handler = self.handlers.get(call.name)
        if not handler:
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                success=False,
                error="tool handler is unavailable",
            )
        try:
            output = await handler(call.arguments)
            return ToolResult(call_id=call.call_id, name=call.name, success=True, output=output)
        except Exception as exc:  # noqa: BLE001 - tool failures are converted to ToolResult
            return ToolResult(call_id=call.call_id, name=call.name, success=False, error=str(exc))

    def _rejected(self, batch: ToolBatch, reason: str) -> list[ToolResult]:
        return [
            ToolResult(call_id=call.call_id, name=call.name, success=False, error=reason)
            for call in batch.calls
        ]

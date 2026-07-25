from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ApprovalDecision(StrEnum):
    APPROVE_ONCE = "APPROVE_ONCE"
    REJECT = "REJECT"


class AuthorizationStatus(StrEnum):
    APPROVED = "APPROVED"
    REQUIRES_APPROVAL = "REQUIRES_APPROVAL"
    DENIED = "DENIED"


class ToolDescriptor(BaseModel):
    name: str
    version: str = "1"
    requires_approval: bool = False
    description: str = ""


class ToolCall(BaseModel):
    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolBatch(BaseModel):
    calls: list[ToolCall]

    @property
    def batch_hash(self) -> str:
        payload = [call.model_dump(mode="json") for call in self.calls]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


class AuthorizationResult(BaseModel):
    status: AuthorizationStatus
    batch_hash: str
    approval_id: str | None = None
    reason: str | None = None


class ToolResult(BaseModel):
    call_id: str
    name: str
    success: bool
    output: Any = None
    error: str | None = None


class ToolGatewayPolicy(BaseModel):
    allowed_tools: set[str] = Field(default_factory=set)
    approval_required_tools: set[str] = Field(default_factory=set)


class ToolGatewayControlPlane:
    """Authorization and deterministic checkpoint boundary; execution stays in the runner."""

    def __init__(self, descriptors: list[ToolDescriptor]) -> None:
        self._descriptors = {descriptor.name: descriptor for descriptor in descriptors}

    def describe_tools(self) -> list[ToolDescriptor]:
        return list(self._descriptors.values())

    def authorize(self, batch: ToolBatch, policy: ToolGatewayPolicy) -> AuthorizationResult:
        unknown = [call.name for call in batch.calls if call.name not in self._descriptors]
        denied = [call.name for call in batch.calls if call.name not in policy.allowed_tools]
        if unknown or denied:
            return AuthorizationResult(
                status=AuthorizationStatus.DENIED,
                batch_hash=batch.batch_hash,
                reason=f"tools are not authorized: {sorted(set(unknown + denied))}",
            )
        requires_approval = any(
            self._descriptors[call.name].requires_approval
            or call.name in policy.approval_required_tools
            for call in batch.calls
        )
        if requires_approval:
            approval_id = hashlib.sha256(f"approval:{batch.batch_hash}".encode()).hexdigest()[:32]
            return AuthorizationResult(
                status=AuthorizationStatus.REQUIRES_APPROVAL,
                batch_hash=batch.batch_hash,
                approval_id=approval_id,
            )
        return AuthorizationResult(status=AuthorizationStatus.APPROVED, batch_hash=batch.batch_hash)

    def checkpoint(self, batch: ToolBatch, authorization: AuthorizationResult) -> dict[str, Any]:
        if authorization.batch_hash != batch.batch_hash:
            raise ValueError("tool batch hash mismatch")
        return {
            "tool_batch": batch.model_dump(mode="json"),
            "tool_batch_hash": batch.batch_hash,
            "authorization": authorization.model_dump(mode="json"),
        }

    def restore(self, checkpoint: dict[str, Any]) -> tuple[ToolBatch, AuthorizationResult]:
        batch = ToolBatch.model_validate(checkpoint["tool_batch"])
        authorization = AuthorizationResult.model_validate(checkpoint["authorization"])
        expected = checkpoint["tool_batch_hash"]
        if batch.batch_hash != expected or authorization.batch_hash != expected:
            raise ValueError("tool batch hash mismatch")
        return batch, authorization

"""Stable wire contracts shared by AgentSupport and Session Runner."""

from .checkpoint import (
    Checkpoint,
    ContextBundle,
    context_bundle_hash,
    tool_policy_hash,
    tool_versions_hash,
    validate_checkpoint,
)
from .events import EventEnvelope
from .execution import (
    ApprovalRequest,
    CheckpointRequest,
    CommandRequest,
    InputRequest,
    ResumeRequest,
    RunRequest,
)
from .registration import (
    RUNNER_CAPABILITIES,
    RunnerHeartbeat,
    RunnerRegistration,
    RunnerRegistrationRequest,
    RunnerRegistrationResponse,
)
from .tools import (
    ApprovalDecision,
    AuthorizationResult,
    AuthorizationStatus,
    ToolBatch,
    ToolCall,
    ToolDescriptor,
    ToolGatewayControlPlane,
    ToolGatewayPolicy,
    ToolResult,
)

__all__ = [
    "RUNNER_CAPABILITIES",
    "ApprovalDecision",
    "ApprovalRequest",
    "AuthorizationResult",
    "AuthorizationStatus",
    "Checkpoint",
    "CheckpointRequest",
    "CommandRequest",
    "ContextBundle",
    "EventEnvelope",
    "InputRequest",
    "ResumeRequest",
    "RunRequest",
    "RunnerHeartbeat",
    "RunnerRegistration",
    "RunnerRegistrationRequest",
    "RunnerRegistrationResponse",
    "ToolBatch",
    "ToolCall",
    "ToolDescriptor",
    "ToolGatewayControlPlane",
    "ToolGatewayPolicy",
    "ToolResult",
    "context_bundle_hash",
    "tool_policy_hash",
    "tool_versions_hash",
    "validate_checkpoint",
]

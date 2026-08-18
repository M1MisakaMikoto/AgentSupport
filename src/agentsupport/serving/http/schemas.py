"""HTTP request schemas for the v0.2 public API (execution resources only)."""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ConfigSkillInput(BaseModel):
    skill_id: str = Field(min_length=1, max_length=120)
    enabled: bool = True


class ConfigToolPolicyInput(BaseModel):
    allowed_tools: list[str] = Field(default_factory=list)
    approval_required_tools: list[str] = Field(default_factory=list)
    tool_descriptors: list[dict[str, Any]] = Field(default_factory=list)


class ConfigResourcesInput(BaseModel):
    mcp_refs: list[dict[str, Any]] = Field(default_factory=list)
    workspace_template: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


class ConfigPermissionsInput(BaseModel):
    max_active_sessions: int | None = None
    allow_network: bool = True
    allow_workspace_write: bool = True


class SessionConfigInput(BaseModel):
    version: int = 1
    skills: list[ConfigSkillInput] = Field(default_factory=list)
    tool_policy: ConfigToolPolicyInput = Field(default_factory=ConfigToolPolicyInput)
    resources: ConfigResourcesInput = Field(default_factory=ConfigResourcesInput)
    permissions: ConfigPermissionsInput = Field(default_factory=ConfigPermissionsInput)


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class WorkspaceVersionCreate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)


class SessionCreate(BaseModel):
    workspace_id: UUID
    name: str | None = Field(default=None, min_length=1, max_length=120)
    tenant_id: str | None = Field(default=None, max_length=120)
    user_id: str | None = Field(default=None, max_length=120)
    project_id: str | None = Field(default=None, max_length=120)
    metadata: dict[str, Any] = Field(default_factory=dict)
    config: SessionConfigInput | None = None


class ConversationCreate(BaseModel):
    task: str = Field(min_length=1)
    parent_conversation_id: UUID | None = None
    workspace_id: UUID | None = None
    skills: list[ConfigSkillInput] | None = None
    mcp_refs: list[dict[str, Any]] | None = None


class McpServerCreate(BaseModel):
    server_id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=120)
    transport: Literal["http", "sse"]
    http_url: str | None = None
    sse_url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    description: str = ""
    enabled: bool = True


class McpServerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    http_url: str | None = None
    sse_url: str | None = None
    headers: dict[str, str] | None = None
    description: str | None = None
    enabled: bool | None = None


class InteractionRequest(BaseModel):
    interaction_id: str
    value: Any
    expected_seq: int | None = None


class ApprovalRequest(BaseModel):
    approval_id: str
    decision: str
    expected_seq: int | None = None


class CancelRequest(BaseModel):
    expected_seq: int | None = None


__all__ = [
    "ApprovalRequest",
    "CancelRequest",
    "ConfigPermissionsInput",
    "ConfigResourcesInput",
    "ConfigSkillInput",
    "ConfigToolPolicyInput",
    "ConversationCreate",
    "InteractionRequest",
    "McpServerCreate",
    "McpServerUpdate",
    "SessionConfigInput",
    "SessionCreate",
    "WorkspaceCreate",
]

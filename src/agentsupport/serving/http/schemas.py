from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    organization_id: UUID | None = None


class OrganizationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class PresetSkillInput(BaseModel):
    skill_id: str = Field(min_length=1, max_length=120)
    enabled: bool = True


class PresetToolPolicyInput(BaseModel):
    allowed_tools: list[str] = Field(default_factory=list)
    approval_required_tools: list[str] = Field(default_factory=list)
    tool_descriptors: list[dict[str, Any]] = Field(default_factory=list)


class PresetResourcesInput(BaseModel):
    mcp_refs: list[dict[str, Any]] = Field(default_factory=list)
    workspace_template: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


class PresetPermissionsInput(BaseModel):
    max_active_sessions: int | None = None
    allow_network: bool = True
    allow_workspace_write: bool = True


class PresetDefinitionInput(BaseModel):
    version: int = 1
    skills: list[PresetSkillInput] = Field(default_factory=list)
    tool_policy: PresetToolPolicyInput = Field(default_factory=PresetToolPolicyInput)
    resources: PresetResourcesInput = Field(default_factory=PresetResourcesInput)
    permissions: PresetPermissionsInput = Field(default_factory=PresetPermissionsInput)
    enabled: bool = True


class PresetCreate(BaseModel):
    user_id: UUID
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    definition: PresetDefinitionInput | None = None


class PresetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    definition: PresetDefinitionInput | None = None


class ProjectCreate(BaseModel):
    user_id: UUID
    name: str = Field(min_length=1, max_length=120)
    preset_id: UUID | None = None


class ProjectConfigInput(BaseModel):
    version: int = 1
    skills: list[PresetSkillInput] = Field(default_factory=list)
    tool_policy: PresetToolPolicyInput = Field(default_factory=PresetToolPolicyInput)
    resources: PresetResourcesInput = Field(default_factory=PresetResourcesInput)
    permissions: PresetPermissionsInput = Field(default_factory=PresetPermissionsInput)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    config: ProjectConfigInput | None = None


class ProjectImportPreset(BaseModel):
    preset_id: UUID


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class SessionCreate(BaseModel):
    workspace_id: UUID
    project_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)


class ConversationCreate(BaseModel):
    task: str = Field(min_length=1)
    parent_conversation_id: UUID | None = None
    workspace_id: UUID | None = None
    project_id: UUID | None = None


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

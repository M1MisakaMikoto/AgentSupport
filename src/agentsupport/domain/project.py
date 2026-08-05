from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from .execution import utc_now


class User(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    organization_id: UUID
    username: str
    created_at: datetime = Field(default_factory=utc_now)


class PresetSkill(BaseModel):
    skill_id: str
    enabled: bool = True


class PresetToolPolicy(BaseModel):
    allowed_tools: list[str] = Field(default_factory=list)
    approval_required_tools: list[str] = Field(default_factory=list)
    tool_descriptors: list[dict[str, Any]] = Field(default_factory=list)


class PresetResources(BaseModel):
    mcp_refs: list[dict[str, Any]] = Field(default_factory=list)
    workspace_template: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


class PresetPermissions(BaseModel):
    max_active_sessions: int | None = None
    allow_network: bool = True
    allow_workspace_write: bool = True


class PresetDefinition(BaseModel):
    version: int = 1
    skills: list[PresetSkill] = Field(default_factory=list)
    tool_policy: PresetToolPolicy = Field(default_factory=PresetToolPolicy)
    resources: PresetResources = Field(default_factory=PresetResources)
    permissions: PresetPermissions = Field(default_factory=PresetPermissions)
    enabled: bool = True


class Preset(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    organization_id: UUID
    user_id: UUID
    name: str
    description: str = ""
    definition: PresetDefinition = Field(default_factory=PresetDefinition)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ProjectConfig(BaseModel):
    """Snapshot of a preset (or deployment defaults) applied to a project."""

    version: int = 1
    skills: list[PresetSkill] = Field(default_factory=list)
    tool_policy: PresetToolPolicy = Field(default_factory=PresetToolPolicy)
    resources: PresetResources = Field(default_factory=PresetResources)
    permissions: PresetPermissions = Field(default_factory=PresetPermissions)

    def is_empty(self) -> bool:
        return not (
            self.skills
            or self.tool_policy.allowed_tools
            or self.tool_policy.approval_required_tools
            or self.tool_policy.tool_descriptors
            or self.resources.mcp_refs
            or self.resources.workspace_template
            or self.resources.env
        )

    def enabled_skill_ids(self) -> list[str]:
        return [skill.skill_id for skill in self.skills if skill.enabled]

    def tool_policy_dict(self) -> dict[str, Any]:
        policy: dict[str, Any] = {
            "allowed_tools": list(self.tool_policy.allowed_tools),
            "approval_required_tools": list(self.tool_policy.approval_required_tools),
        }
        if self.tool_policy.tool_descriptors:
            policy["tool_descriptors"] = [dict(item) for item in self.tool_policy.tool_descriptors]
        return policy


class Project(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    organization_id: UUID
    user_id: UUID
    workspace_id: UUID
    name: str
    preset_id: UUID | None = None
    config: ProjectConfig = Field(default_factory=ProjectConfig)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


def project_config_from_definition(definition: PresetDefinition) -> ProjectConfig:
    """Snapshot a preset definition into a project config (copy, not reference)."""

    return ProjectConfig(
        version=definition.version,
        skills=[PresetSkill(skill_id=skill.skill_id, enabled=skill.enabled) for skill in definition.skills],
        tool_policy=PresetToolPolicy(
            allowed_tools=list(definition.tool_policy.allowed_tools),
            approval_required_tools=list(definition.tool_policy.approval_required_tools),
            tool_descriptors=[dict(item) for item in definition.tool_policy.tool_descriptors],
        ),
        resources=PresetResources(
            mcp_refs=[dict(item) for item in definition.resources.mcp_refs],
            workspace_template=definition.resources.workspace_template,
            env=dict(definition.resources.env),
        ),
        permissions=PresetPermissions(
            max_active_sessions=definition.permissions.max_active_sessions,
            allow_network=definition.permissions.allow_network,
            allow_workspace_write=definition.permissions.allow_workspace_write,
        ),
    )


def default_project_config(
    *, enabled_skills: list[str], tool_policy: dict[str, Any] | None = None
) -> ProjectConfig:
    """Deployment-default project config used when a project is created without a preset."""

    policy = tool_policy or {
        "allowed_tools": [
            "bash",
            "str_replace_based_edit_tool",
            "json_edit_tool",
            "sequentialthinking",
            "task_done",
        ],
        "approval_required_tools": [
            "bash",
            "str_replace_based_edit_tool",
            "json_edit_tool",
        ],
    }
    return ProjectConfig(
        skills=[PresetSkill(skill_id=skill_id, enabled=True) for skill_id in enabled_skills],
        tool_policy=PresetToolPolicy(
            allowed_tools=list(policy.get("allowed_tools", [])),
            approval_required_tools=list(policy.get("approval_required_tools", [])),
            tool_descriptors=[
                dict(item) for item in policy.get("tool_descriptors", [])
            ],
        ),
    )

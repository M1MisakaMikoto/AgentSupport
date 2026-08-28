"""Skill generation domain models.

A generation request turns a finished session's event history into a
SKILL.md draft through an agent run; the draft then needs human review
before it is published into the (tenant-scoped) skill library.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from .execution import utc_now


class DraftStatus(StrEnum):
    DRAFT = "draft"
    REVIEW = "review"
    PUBLISHED = "published"
    REJECTED = "rejected"


class GenerationStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class SkillDraft(BaseModel):
    """A generated SKILL.md awaiting human review before publication."""

    id: UUID = Field(default_factory=uuid4)
    skill_id: str
    tenant_id: str | None = None
    project_id: str | None = None
    source_session_id: UUID
    source_conversation_id: UUID
    generation_id: UUID
    status: DraftStatus = DraftStatus.DRAFT
    skill_content: str
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    reviewed_at: datetime | None = None
    review_note: str | None = None


class SkillGenerationRequest(BaseModel):
    """Manual skill-generation request backed by an agent run."""

    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    conversation_id: UUID | None = None
    tenant_id: str | None = None
    project_id: str | None = None
    status: GenerationStatus = GenerationStatus.PENDING
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None

"""Session operations: listing, lookup, creation and container acquisition."""



from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from ..domain import (
    ProjectConfig,
    Session,
)
from .common import (
    ServiceError,
    _hash_request,
)
from .ports import (
    RepositoryConflict,
)


class SessionOpsMixin:

    def list_sessions(
        self,
        *,
        workspace_id: UUID | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        project_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Session]:
        if self.repository:
            return self.repository.list_sessions(
                tenant_id=tenant_id,
                user_id=user_id,
                project_id=project_id,
                limit=limit,
                offset=offset,
            )
        items = self.sessions.values()
        if workspace_id is not None:
            items = [item for item in items if item.workspace_id == workspace_id]
        if tenant_id is not None:
            items = [item for item in items if item.tenant_id == tenant_id]
        if user_id is not None:
            items = [item for item in items if item.user_id == user_id]
        if project_id is not None:
            items = [item for item in items if item.project_id == project_id]
        items = sorted(items, key=lambda item: item.created_at)
        if limit is not None:
            items = items[offset : offset + limit]
        return items


    def get_session(self, session_id: UUID) -> Session:
        session = None if self.temporal_mode else self.sessions.get(session_id)
        if not session and self.repository:
            session = self.repository.get_session(session_id)
            if session:
                self.sessions[session.id] = session
        if not session:
            raise ServiceError("SESSION_NOT_FOUND", "session does not exist", 404)
        return session


    def create_session(
        self,
        workspace_id: UUID,
        idempotency_key: str | None = None,
        *,
        session_id: UUID | None = None,
        name: str | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        project_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        config: ProjectConfig | None = None,
        auto_created: dict[str, Any] | None = None,
    ) -> Session:
        if config is not None and config.skills:
            self._validate_skill_ids(
                [skill.skill_id for skill in config.skills], tenant_id=tenant_id
            )
        if config is not None and config.resources.mcp_refs:
            self._validate_mcp_refs(config.resources.mcp_refs)
        workspace = self._resolve_workspace(
            workspace_id, name=name, auto_created=auto_created
        )
        if session_id is not None:
            existing = None if self.temporal_mode else self.sessions.get(session_id)
            if not existing and self.repository:
                existing = self.repository.get_session(session_id)
                if existing:
                    self.sessions[existing.id] = existing
            if existing:
                return existing
        payload = {
            "workspace_id": workspace_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "project_id": project_id,
        }
        existing = self._idempotent("session", idempotency_key, payload)
        if existing:
            return self.sessions[existing]
        session = (
            Session(
                id=session_id,
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                user_id=user_id,
                project_id=project_id,
                metadata=metadata or {},
                config=config,
            )
            if session_id is not None
            else Session(
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                user_id=user_id,
                project_id=project_id,
                metadata=metadata or {},
                config=config,
            )
        )
        if self.repository:
            try:
                session = self.repository.create_session(
                    workspace,
                    _hash_request(payload),
                    idempotency_key,
                    project_id=project_id,
                    session_id=session_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    metadata=metadata,
                    config=config,
                )
            except RepositoryConflict as exc:
                raise ServiceError("IDEMPOTENCY_CONFLICT", str(exc), 409) from exc
        self.sessions[session.id] = session
        if auto_created is not None:
            auto_created["session"] = session.model_dump(mode="json")
        self._remember("session", idempotency_key, payload, session.id)
        return session





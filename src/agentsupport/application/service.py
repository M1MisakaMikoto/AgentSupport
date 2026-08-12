from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import socket
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4

from agent_runner_contracts.checkpoint import (
    tool_policy_hash,
    tool_versions_hash,
    validate_checkpoint,
)
from agent_runner_contracts.events import EventEnvelope
from agent_runner_contracts.registration import (
    RunnerHeartbeat,
    RunnerRegistration,
    RunnerRegistrationRequest,
    RunnerRegistrationResponse,
)
from agent_runner_contracts.tools import ToolBatch

from ..domain import (
    SUPPORTED_TRANSPORTS,
    TERMINAL_STATES,
    Checkpoint,
    ContextBundle,
    Conversation,
    ExecutionState,
    McpServer,
    PresetSkill,
    ProjectConfig,
    Session,
    Workspace,
)
from ..observability import metrics as obs_metrics
from .ports import (
    CoreRuntime,
    EventNotifier,
    EventStore,
    RepositoryConflict,
    RunnerRegistry,
    RuntimeDriver,
    SkillProvider,
    WorkspaceProvider,
)
from .runner_registry import InMemoryRunnerRegistry, RunnerRegistryConflict


class ServiceError(Exception):
    def __init__(
        self, code: str, message: str, status_code: int = 400, details: dict[str, Any] | None = None
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


def _hash_request(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


_MCP_SERVER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass
class IdempotencyRecord:
    request_hash: str
    resource_id: UUID
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class AgentSupportService:
    """AgentSupport application service with replaceable persistence ports."""

    def __init__(
        self,
        config: Any,
        *,
        events_store: EventStore,
        event_notifier: EventNotifier,
        workspace_provider: WorkspaceProvider,
        skill_provider: SkillProvider,
        runtime_driver: RuntimeDriver,
        core_runtime: CoreRuntime | None,
        repository: Any | None,
        runner_registry: RunnerRegistry | None = None,
    ) -> None:
        self.config = config
        self.distributed = config.execution_mode == "distributed"
        self.instance_id = config.instance_id or f"{socket.gethostname()}:{os.getpid()}"
        self.workspaces: dict[UUID, Workspace] = {}
        self.sessions: dict[UUID, Session] = {}
        self.conversations: dict[UUID, Conversation] = {}
        self.checkpoints: dict[UUID, Checkpoint] = {}
        self.events_store = events_store
        self.event_notifier = event_notifier
        self.workspace_provider = workspace_provider
        self.skill_provider = skill_provider
        self.enabled_skills = [
            item.strip() for item in config.enabled_skills.split(",") if item.strip()
        ]
        self.runtime_driver = runtime_driver
        self.core_runtime = core_runtime
        self.idempotency: dict[tuple[str, str], IdempotencyRecord] = {}
        self.workspace_leases: dict[UUID, UUID] = {}
        self._health_failures: dict[UUID, int] = {}
        self._scheduler_lock = asyncio.Lock()
        self.repository = repository
        self.runner_registry = runner_registry or InMemoryRunnerRegistry()
        self.mcp_servers: dict[str, McpServer] = {}
        if self.distributed and self.repository is None:
            raise ValueError("distributed execution requires PostgreSQL persistence")
        if self.repository and not self.distributed:
            self._load_persisted_state()

    def _load_persisted_state(self) -> None:
        assert self.repository is not None
        self.workspaces = {item.id: item for item in self.repository.list_workspaces()}
        self.sessions = {item.id: item for item in self.repository.list_sessions()}
        self.conversations = {item.id: item for item in self.repository.list_conversations()}
        for session in self.sessions.values():
            if session.active_container_id:
                self.workspace_leases[session.workspace_id] = session.id
        for conversation in self.conversations.values():
            for event in self.repository.list_events(conversation.id):
                self.events_store.append(conversation.id, event)

    def _idempotent(self, scope: str, key: str | None, payload: dict[str, Any]) -> UUID | None:
        if not key:
            return None
        record = self.idempotency.get((scope, key))
        request_hash = _hash_request(payload)
        if record:
            if record.request_hash != request_hash:
                raise ServiceError(
                    "IDEMPOTENCY_CONFLICT",
                    "idempotency key was reused with a different request",
                    409,
                )
            return record.resource_id
        return None

    async def _runner_endpoint(self, session: Session) -> str | None:
        if session.active_container_id:
            endpoint_method = getattr(self.runtime_driver, "endpoint", None)
            if endpoint_method is not None:
                endpoint = await endpoint_method(session.active_container_id)
                if endpoint:
                    return endpoint
        return self.config.core_runner_url

    async def _register_core_endpoint(
        self, session: Session, run_id: UUID, endpoint: str | None = None
    ) -> str | None:
        if endpoint is None:
            endpoint = await self._runner_endpoint(session)
        if self.core_runtime is not None:
            register = getattr(self.core_runtime, "register_run_endpoint", None)
            if register is not None:
                register(run_id, endpoint)
        return endpoint

    @staticmethod
    def _tool_policy() -> dict[str, Any]:
        return {
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

    # ------------------------------------------------------------------
    # Session configuration resolution (config travels with the session)
    # ------------------------------------------------------------------

    def _skills_for_session(self, session: Session) -> list[str]:
        if session.config and not session.config.is_empty():
            return session.config.enabled_skill_ids()
        return self.enabled_skills

    def _skills_for_conversation(
        self, conversation: Conversation, session: Session
    ) -> list[str]:
        if conversation.skills is not None:
            return [skill.skill_id for skill in conversation.skills if skill.enabled]
        return self._skills_for_session(session)

    def _tool_policy_for_session(self, session: Session) -> dict[str, Any]:
        if session.config and not session.config.is_empty():
            policy = session.config.tool_policy_dict()
            if policy.get("allowed_tools") or policy.get("approval_required_tools"):
                return policy
        return self._tool_policy()

    # ------------------------------------------------------------------
    # Missing-precondition auto-completion (configurable, default ON)
    # ------------------------------------------------------------------

    def _auto_create(self, scope: str) -> bool:
        """Whether missing resources of ``scope`` may be auto-created."""

        if not getattr(self.config, "auto_create_missing", False):
            return False
        scopes = getattr(self.config, "auto_create_scopes_set", None)
        if scopes is None:
            raw = getattr(self.config, "auto_create_scopes", "all")
            scopes = (
                {"all"}
                if str(raw).strip() == "all"
                else {item.strip() for item in str(raw).split(",") if item.strip()}
            )
        return "all" in scopes or scope in scopes

    def _resolve_workspace(
        self,
        workspace_id: UUID,
        *,
        name: str | None = None,
        auto_created: dict[str, Any] | None = None,
    ) -> Workspace:
        workspace = None if self.distributed else self.workspaces.get(workspace_id)
        if not workspace and self.repository:
            workspace = self.repository.get_workspace(workspace_id)
            if workspace:
                self.workspaces[workspace.id] = workspace
        if workspace:
            return workspace
        if not self._auto_create("workspace"):
            raise ServiceError("WORKSPACE_NOT_FOUND", "workspace does not exist", 404)
        return self.create_workspace(
            name or "auto",
            idempotency_key=None,
            workspace_id=workspace_id,
            auto_created=auto_created,
        )

    def list_sessions(
        self,
        *,
        workspace_id: UUID | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        project_id: str | None = None,
    ) -> list[Session]:
        if self.repository:
            return self.repository.list_sessions(
                tenant_id=tenant_id, user_id=user_id, project_id=project_id
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
        return sorted(items, key=lambda item: item.created_at)

    def get_session(self, session_id: UUID) -> Session:
        session = None if self.distributed else self.sessions.get(session_id)
        if not session and self.repository:
            session = self.repository.get_session(session_id)
            if session:
                self.sessions[session.id] = session
        if not session:
            raise ServiceError("SESSION_NOT_FOUND", "session does not exist", 404)
        return session

    def get_conversation(self, conversation_id: UUID) -> Conversation:
        return self._conversation(conversation_id)

    def list_conversations(self, session_id: UUID | None = None) -> list[Conversation]:
        if self.repository:
            return self.repository.list_conversations(session_id=session_id)
        items = self.conversations.values()
        if session_id is not None:
            items = [item for item in items if item.session_id == session_id]
        return sorted(items, key=lambda item: item.created_at)

    def get_workspace(self, workspace_id: UUID) -> Workspace:
        workspace = None if self.distributed else self.workspaces.get(workspace_id)
        if not workspace and self.repository:
            workspace = self.repository.get_workspace(workspace_id)
            if workspace:
                self.workspaces[workspace.id] = workspace
        if not workspace:
            raise ServiceError("WORKSPACE_NOT_FOUND", "workspace does not exist", 404)
        return workspace

    def list_workspaces(self) -> list[Workspace]:
        if self.repository:
            return self.repository.list_workspaces()
        return sorted(self.workspaces.values(), key=lambda item: item.created_at)

    def _remember(
        self, scope: str, key: str | None, payload: dict[str, Any], resource_id: UUID
    ) -> None:
        if key:
            self.idempotency[(scope, key)] = IdempotencyRecord(_hash_request(payload), resource_id)

    def _remember_persisted(
        self,
        scope: str,
        key: str | None,
        payload: dict[str, Any],
        resource_id: UUID,
        response: dict[str, Any],
    ) -> None:
        self._remember(scope, key, payload, resource_id)
        if self.repository and key:
            try:
                self.repository.remember_idempotent(
                    scope, key, _hash_request(payload), resource_id, response
                )
            except RepositoryConflict as exc:
                raise ServiceError("IDEMPOTENCY_CONFLICT", str(exc), 409) from exc

    def create_workspace(
        self,
        name: str,
        idempotency_key: str | None = None,
        *,
        workspace_id: UUID | None = None,
        auto_created: dict[str, Any] | None = None,
    ) -> Workspace:
        if workspace_id is not None:
            existing = None if self.distributed else self.workspaces.get(workspace_id)
            if not existing and self.repository:
                existing = self.repository.get_workspace(workspace_id)
                if existing:
                    self.workspaces[existing.id] = existing
            if existing:
                return existing
        payload = {"name": name}
        existing = self._idempotent("workspace", idempotency_key, payload)
        if existing:
            return self.workspaces[existing]
        if self.repository and idempotency_key:
            try:
                existing = self.repository.find_idempotent(
                    "workspace", idempotency_key, _hash_request(payload)
                )
            except RepositoryConflict as exc:
                raise ServiceError("IDEMPOTENCY_CONFLICT", str(exc), 409) from exc
            if existing:
                workspace = self.repository.get_workspace(existing)
                if workspace:
                    self.workspaces[workspace.id] = workspace
                    self._remember("workspace", idempotency_key, payload, workspace.id)
                    return workspace
        workspace_id, path = self.workspace_provider.create(name, workspace_id)
        workspace = Workspace(id=workspace_id, name=name, root_path=path)
        if self.repository:
            try:
                workspace = self.repository.create_workspace(
                    name,
                    path,
                    _hash_request(payload),
                    idempotency_key,
                    workspace_id=workspace_id,
                )
            except RepositoryConflict as exc:
                raise ServiceError("IDEMPOTENCY_CONFLICT", str(exc), 409) from exc
        self.workspaces[workspace.id] = workspace
        if auto_created is not None:
            auto_created["workspace"] = workspace.model_dump(mode="json")
        self._remember("workspace", idempotency_key, payload, workspace.id)
        return workspace

    def list_skills(self) -> list[dict[str, Any]]:
        return self.skill_provider.list_skills()

    def get_skill(self, skill_id: str) -> dict[str, Any]:
        try:
            return self.skill_provider.describe_skill(skill_id)
        except (FileNotFoundError, ValueError) as exc:
            raise ServiceError("SKILL_NOT_FOUND", str(exc), 404) from exc

    def create_skill(
        self, skill_id: str, *, filename: str, payload: bytes
    ) -> dict[str, Any]:
        try:
            if filename.lower().endswith(".zip"):
                return self.skill_provider.install_zip(skill_id, payload)
            return self.skill_provider.install_skill(skill_id, {"SKILL.md": payload})
        except ValueError as exc:
            raise ServiceError("SKILL_INVALID_PAYLOAD", str(exc), 422) from exc

    def delete_skill(self, skill_id: str) -> None:
        try:
            removed = self.skill_provider.remove_skill(skill_id)
        except ValueError as exc:
            raise ServiceError("SKILL_INVALID_PAYLOAD", str(exc), 422) from exc
        if not removed:
            raise ServiceError("SKILL_NOT_FOUND", "skill does not exist", 404)

    def _validate_skill_ids(self, skill_ids: list[str]) -> None:
        if not skill_ids:
            return
        try:
            self.skill_provider.resolve(skill_ids)
        except (FileNotFoundError, ValueError) as exc:
            raise ServiceError("SKILL_NOT_FOUND", str(exc), 404) from exc

    def _mcp_server_id(self, ref: Any) -> str:
        if isinstance(ref, str):
            return ref
        if isinstance(ref, dict):
            server_id = ref.get("server_id")
            if isinstance(server_id, str) and server_id:
                return server_id
        raise ServiceError("MCP_REF_INVALID", "mcp reference must carry server_id", 422)

    def _validate_mcp_refs(self, refs: list[Any]) -> None:
        for ref in refs:
            server_id = self._mcp_server_id(ref)
            server = self.get_mcp_server(server_id)
            if not server.enabled:
                raise ServiceError(
                    "MCP_SERVER_DISABLED",
                    f"mcp server is disabled: {server_id}",
                    422,
                )

    def get_mcp_server(self, server_id: str) -> McpServer:
        if self.repository:
            server = self.repository.get_mcp_server(server_id)
            if server:
                self.mcp_servers[server.server_id] = server
        else:
            server = self.mcp_servers.get(server_id)
        if not server:
            raise ServiceError("MCP_SERVER_NOT_FOUND", "mcp server does not exist", 404)
        return server

    def list_mcp_servers(self) -> list[McpServer]:
        if self.repository:
            return self.repository.list_mcp_servers()
        return sorted(self.mcp_servers.values(), key=lambda item: item.server_id)

    def create_mcp_server(
        self,
        *,
        server_id: str,
        name: str,
        transport: str,
        http_url: str | None = None,
        sse_url: str | None = None,
        headers: dict[str, str] | None = None,
        description: str = "",
        enabled: bool = True,
    ) -> McpServer:
        if not _MCP_SERVER_ID.fullmatch(server_id):
            raise ServiceError("MCP_SERVER_INVALID", "invalid mcp server id", 422)
        if transport not in SUPPORTED_TRANSPORTS:
            raise ServiceError(
                "MCP_TRANSPORT_UNSUPPORTED",
                f"unsupported transport: {transport}",
                422,
            )
        if transport == "http" and not http_url:
            raise ServiceError("MCP_SERVER_INVALID", "http transport requires http_url", 422)
        if transport == "sse" and not sse_url:
            raise ServiceError("MCP_SERVER_INVALID", "sse transport requires sse_url", 422)
        server = McpServer(
            server_id=server_id,
            name=name,
            transport=transport,
            http_url=http_url,
            sse_url=sse_url,
            headers=dict(headers or {}),
            description=description,
            enabled=enabled,
        )
        if self.repository:
            self.repository.save_mcp_server(server)
        self.mcp_servers[server.server_id] = server
        return server

    def update_mcp_server(
        self,
        server_id: str,
        *,
        name: str | None = None,
        http_url: str | None = None,
        sse_url: str | None = None,
        headers: dict[str, str] | None = None,
        description: str | None = None,
        enabled: bool | None = None,
    ) -> McpServer:
        current = self.get_mcp_server(server_id)
        updated = current.model_copy(
            update={
                "name": name if name is not None else current.name,
                "http_url": http_url if http_url is not None else current.http_url,
                "sse_url": sse_url if sse_url is not None else current.sse_url,
                "headers": dict(headers) if headers is not None else dict(current.headers),
                "description": description if description is not None else current.description,
                "enabled": enabled if enabled is not None else current.enabled,
            }
        )
        if self.repository:
            self.repository.save_mcp_server(updated)
        self.mcp_servers[updated.server_id] = updated
        return updated

    def delete_mcp_server(self, server_id: str) -> None:
        if self.repository:
            removed = self.repository.delete_mcp_server(server_id)
        else:
            removed = self.mcp_servers.pop(server_id, None) is not None
        if not removed:
            raise ServiceError("MCP_SERVER_NOT_FOUND", "mcp server does not exist", 404)

    def _resolve_mcp_refs(
        self, refs: list[Any] | None, session: Session
    ) -> list[dict[str, Any]]:
        if refs is None:
            refs = (
                session.config.resources.mcp_refs
                if session.config is not None and session.config.resources.mcp_refs
                else []
            )
        resolved: list[dict[str, Any]] = []
        for ref in refs:
            server_id = self._mcp_server_id(ref)
            server = self.get_mcp_server(server_id)
            resolved.append(
                {
                    "server_id": server.server_id,
                    "transport": server.transport,
                    "http_url": server.http_url,
                    "sse_url": server.sse_url,
                    "headers": dict(server.headers),
                    "description": server.description,
                }
            )
        return resolved

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
            self._validate_skill_ids([skill.skill_id for skill in config.skills])
        if config is not None and config.resources.mcp_refs:
            self._validate_mcp_refs(config.resources.mcp_refs)
        workspace = self._resolve_workspace(
            workspace_id, name=name, auto_created=auto_created
        )
        if session_id is not None:
            existing = None if self.distributed else self.sessions.get(session_id)
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

    def _append(
        self,
        conversation: Conversation,
        event_type: str,
        payload: dict[str, Any],
        source: str = "agentsupport",
    ) -> EventEnvelope:
        seq = conversation.run.last_seq + 1
        tenant_id, user_id, project_id = self._session_labels(conversation.session_id)
        event = EventEnvelope(
            run_id=conversation.run.run_id,
            seq=seq,
            type=event_type,
            payload=payload,
            tenant_id=tenant_id,
            user_id=user_id,
            project_id=project_id,
            source=source,
        )
        if self.repository:
            try:
                self.repository.append_event(conversation, event)
            except RepositoryConflict as exc:
                raise ServiceError("EVENT_CONFLICT", str(exc), 409) from exc
        self.events_store.append(conversation.id, event)
        conversation.run.last_seq = seq
        try:
            asyncio.get_running_loop().create_task(
                self.events_store.publish(conversation.id, event)
            )
        except RuntimeError:
            pass
        return event

    def _session_labels(self, session_id: UUID) -> tuple[str | None, str | None, str | None]:
        session = None if self.distributed else self.sessions.get(session_id)
        if not session and self.repository:
            session = self.repository.get_session(session_id)
        if not session:
            return None, None, None
        return session.tenant_id, session.user_id, session.project_id

    async def _acquire_container(self, session: Session) -> bool:
        async with self._scheduler_lock:
            return await self._acquire_container_locked(session)

    async def _acquire_container_locked(self, session: Session) -> bool:
        active = sum(1 for item in self.sessions.values() if item.active_container_id)
        if self.repository:
            active = max(active, self.repository.active_container_count())
        if active >= self.config.max_active_sessions:
            return False
        lease_holder = self.workspace_leases.get(session.workspace_id)
        if lease_holder is not None and lease_holder != session.id:
            return False
        workspace = self.workspaces.get(session.workspace_id)
        if workspace is None and self.repository:
            workspace = self.repository.get_workspace(session.workspace_id)
            if workspace:
                self.workspaces[workspace.id] = workspace
        if workspace is None:
            raise ServiceError("WORKSPACE_NOT_FOUND", "workspace does not exist", 404)
        session.lease_epoch += 1
        operation_id = (
            self.repository.create_runtime_operation("start", str(session.id))
            if self.repository
            else None
        )
        try:
            session.active_container_id = await asyncio.wait_for(
                self.runtime_driver.start(
                    session.id,
                    workspace.root_path,
                    session.lease_epoch,
                    session.workspace_id,
                    self.skill_provider.read_only_mounts(self._skills_for_session(session)),
                    runtime_operation_id=operation_id,
                ),
                timeout=self.config.runtime_start_timeout_seconds,
            )
        except Exception as exc:
            if self.repository and operation_id:
                self.repository.finish_runtime_operation(
                    operation_id, "FAILED", {"error": str(exc)}
                )
            raise
        if self.repository and not self.repository.try_acquire_workspace_lease(
            session.workspace_id,
            session.id,
            session.lease_epoch,
            session.active_container_id,
            operation_id,
        ):
            await self.runtime_driver.stop(session.active_container_id)
            if operation_id:
                self.repository.finish_runtime_operation(
                    operation_id, "FAILED", {"error": "workspace lease unavailable"}
                )
            session.active_container_id = None
            return False
        if self.repository and operation_id:
            self.repository.finish_runtime_operation(
                operation_id, "SUCCEEDED", {"container_id": session.active_container_id}
            )
        self.workspace_leases[session.workspace_id] = session.id
        if self.repository:
            self.repository.save_session(session)
        return True

    async def create_conversation(
        self,
        session_id: UUID,
        task: str,
        parent_conversation_id: UUID | None = None,
        idempotency_key: str | None = None,
        *,
        workspace_id: UUID | None = None,
        skills: list[PresetSkill] | None = None,
        mcp_refs: list[dict[str, Any]] | None = None,
        auto_created: dict[str, Any] | None = None,
    ) -> Conversation:
        if skills is not None:
            self._validate_skill_ids([skill.skill_id for skill in skills])
        if mcp_refs is not None:
            self._validate_mcp_refs(mcp_refs)
        session = None if self.distributed else self.sessions.get(session_id)
        if not session and self.repository:
            session = self.repository.get_session(session_id)
            if session:
                self.sessions[session.id] = session
        if not session:
            if not self._auto_create("session"):
                raise ServiceError("SESSION_NOT_FOUND", "session does not exist", 404)
            if workspace_id is None:
                raise ServiceError("SESSION_NOT_FOUND", "session does not exist", 404)
            session = self.create_session(
                workspace_id,
                idempotency_key=None,
                session_id=session_id,
                auto_created=auto_created,
            )
        if parent_conversation_id:
            parent = None if self.distributed else self.conversations.get(parent_conversation_id)
            if not parent and self.repository:
                parent = self.repository.get_conversation(parent_conversation_id)
                if parent:
                    self.conversations[parent.id] = parent
            if not parent or parent.session_id != session_id:
                raise ServiceError("PARENT_NOT_FOUND", "parent conversation is invalid", 404)
        payload = {
            "session_id": session_id,
            "task": task,
            "parent_conversation_id": parent_conversation_id,
        }
        existing = self._idempotent("conversation", idempotency_key, payload)
        if existing:
            return self.conversations[existing]
        conversation = Conversation(
            session_id=session_id,
            task=task,
            parent_conversation_id=parent_conversation_id,
            skills=skills,
            mcp_refs=mcp_refs,
        )
        if self.distributed:
            assert self.repository is not None
            if self.repository.queue_depth() >= self.config.max_queued_conversations:
                raise ServiceError("RESOURCE_EXHAUSTED", "conversation queue is full", 429)
            try:
                persisted, _ = self.repository.create_conversation_and_enqueue(
                    conversation,
                    session.workspace_id,
                    _hash_request(payload),
                    idempotency_key,
                    max_attempts=self.config.max_job_attempts,
                )
            except RepositoryConflict as exc:
                raise ServiceError("IDEMPOTENCY_CONFLICT", str(exc), 409) from exc
            return persisted
        if self.repository:
            try:
                persisted = self.repository.create_conversation(
                    conversation, _hash_request(payload), idempotency_key
                )
            except RepositoryConflict as exc:
                raise ServiceError("IDEMPOTENCY_CONFLICT", str(exc), 409) from exc
            if persisted.id != conversation.id:
                self.conversations[persisted.id] = persisted
                return persisted
            conversation = persisted
        self.conversations[conversation.id] = conversation
        if session.active_run_id:
            conversation.run.state = ExecutionState.QUEUED
            self._append(conversation, "conversation.queued", {"session_id": str(session_id)})
        else:
            queued = sum(
                1
                for item in self.conversations.values()
                if item.id != conversation.id and item.run.state == ExecutionState.QUEUED
            )
            if queued >= self.config.max_queued_conversations:
                self.conversations.pop(conversation.id, None)
                if self.repository:
                    self.repository.delete_conversation(conversation.id, idempotency_key)
                raise ServiceError("RESOURCE_EXHAUSTED", "conversation queue is full", 429)
            try:
                acquired = await self._acquire_container(session)
            except TimeoutError:
                conversation.run.state = ExecutionState.FAILED
                self._append(
                    conversation,
                    "run.failed",
                    {
                        "code": "CONTAINER_START_TIMEOUT",
                        "message": "Session container did not start before the configured timeout",
                    },
                )
                self._remember_persisted(
                    "conversation",
                    idempotency_key,
                    payload,
                    conversation.id,
                    conversation.model_dump(mode="json"),
                )
                return conversation
            except Exception as exc:  # noqa: BLE001 - startup errors become run events
                conversation.run.state = ExecutionState.FAILED
                self._append(
                    conversation,
                    "run.failed",
                    {"code": "CONTAINER_START_FAILED", "message": str(exc)},
                )
                self._remember_persisted(
                    "conversation",
                    idempotency_key,
                    payload,
                    conversation.id,
                    conversation.model_dump(mode="json"),
                )
                return conversation
            if not acquired:
                conversation.run.state = ExecutionState.QUEUED
                self._append(conversation, "conversation.queued", {"session_id": str(session_id)})
            else:
                session.active_run_id = conversation.run.run_id
                if self.repository:
                    self.repository.save_session(session)
                conversation.run.state = ExecutionState.STARTING
                self._append(conversation, "run.started", {"session_id": str(session_id)})
                conversation.run.state = ExecutionState.RUNNING
                self._append(
                    conversation, "run.running", {"container_id": session.active_container_id}
                )
                if self.core_runtime:
                    await self._run_core(conversation, session)
        self._remember("conversation", idempotency_key, payload, conversation.id)
        return conversation

    async def _run_core(self, conversation: Conversation, session: Session) -> None:
        started = time.perf_counter()

        async def event_sink(event: EventEnvelope) -> None:
            self._apply_core_event(conversation, event)

        workspace = self.workspaces[session.workspace_id]
        workspace_ref = (
            str(
                PurePosixPath(
                    "/" + str(self.config.core_runner_workspace_root).replace("\\", "/").lstrip("/")
                )
                / Path(workspace.root_path).name
            )
            if self.config.core_runner_workspace_root
            else "/workspace"
        )
        runner_endpoint = await self._runner_endpoint(session)
        registered_runner = self.select_ready_runner({"run"})
        used_registered = False
        if registered_runner and (
            not runner_endpoint or runner_endpoint == self.config.core_runner_url
        ):
            runner_endpoint = registered_runner.endpoint
            used_registered = True
        if used_registered or (runner_endpoint and not self.config.core_runner_url):
            workspace_ref = "/workspace"
        tool_policy = self._tool_policy_for_session(session)
        request = {
            "run_id": str(conversation.run.run_id),
            "conversation_id": str(conversation.id),
            "session_id": str(session.id),
            "container_id": session.active_container_id or "unassigned",
            "lease_epoch": session.lease_epoch,
            "fence_epoch": session.lease_epoch,
            "correlation_id": str(uuid4()),
            "context_bundle": {
                "task": conversation.task,
                "conversation_id": str(conversation.id),
                "workspace_ref": workspace_ref,
                "recent_events": [
                    event.model_dump(mode="json")
                    for event in self.events_store.list(conversation.id)
                ],
                "skill_manifest": self.skill_provider.manifest(
                    self._skills_for_conversation(conversation, session)
                ),
                "tool_policy": tool_policy,
                "mcp_refs": self._resolve_mcp_refs(conversation.mcp_refs, session),
            },
            "workspace_ref": workspace_ref,
            "tool_policy": tool_policy,
            "core_version": "0.1.0",
        }
        runner_endpoint = await self._register_core_endpoint(
            session, conversation.run.run_id, runner_endpoint
        )
        if runner_endpoint:
            request["runner_url"] = runner_endpoint
        try:
            await self.core_runtime.run(request, event_sink)
        except Exception as exc:  # noqa: BLE001 - runtime failures become AgentSupport events
            conversation.run.state = ExecutionState.FAILED
            self._append(
                conversation,
                "run.failed",
                {"code": "CORE_RUNTIME_ERROR", "message": str(exc)},
            )
        if conversation.run.state in TERMINAL_STATES:
            obs_metrics.record_run(
                conversation.run.state.value.lower(), time.perf_counter() - started
            )
            await self._release_session(session, conversation)

    def _apply_core_event(self, conversation: Conversation, event: EventEnvelope) -> None:
        if event.type == "interaction.requested":
            conversation.run.pending_interaction = event.payload
            conversation.run.state = ExecutionState.WAITING_INPUT
        elif event.type == "run.completed":
            conversation.run.pending_interaction = None
            conversation.run.state = ExecutionState.COMPLETED
            conversation.run.result_summary = event.payload.get("result", event.payload)
        elif event.type == "run.failed":
            conversation.run.state = ExecutionState.FAILED
        elif event.type == "run.lost":
            conversation.run.state = ExecutionState.LOST
        elif event.type == "run.cancelled":
            conversation.run.pending_interaction = None
            conversation.run.state = ExecutionState.CANCELLED
        self._append(conversation, event.type, event.payload, source=event.source)

    async def _forward_core_events(
        self, conversation: Conversation, response: dict[str, Any]
    ) -> None:
        for raw_event in response.get("events", []):
            self._apply_core_event(conversation, EventEnvelope.model_validate(raw_event))

    async def _release_session(
        self, session: Session, conversation: Conversation | None = None
    ) -> bool:
        if session.active_container_id:
            operation_id = (
                self.repository.create_runtime_operation("stop", session.active_container_id)
                if self.repository
                else None
            )
            try:
                stopped = await self.runtime_driver.stop(session.active_container_id)
            except Exception as exc:  # noqa: BLE001 - runtime failures keep the lease fenced
                if self.repository and operation_id:
                    self.repository.finish_runtime_operation(
                        operation_id, "FAILED", {"error": str(exc)}
                    )
                stopped = False
            if not stopped:
                if self.repository and operation_id:
                    self.repository.finish_runtime_operation(
                        operation_id, "FAILED", {"error": "container stop unconfirmed"}
                    )
                if conversation:
                    self._append(
                        conversation,
                        "container_stop_unconfirmed",
                        {"container_id": session.active_container_id},
                    )
                return False
            if self.repository and operation_id:
                self.repository.finish_runtime_operation(operation_id, "SUCCEEDED", {})
        session.active_container_id = None
        session.active_run_id = None
        if conversation and self.core_runtime is not None:
            unregister = getattr(self.core_runtime, "unregister_run_endpoint", None)
            if unregister is not None:
                unregister(conversation.run.run_id)
        if self.repository:
            self.repository.release_session_leases(session)
            self.repository.save_session(session)
        if self.workspace_leases.get(session.workspace_id) == session.id:
            self.workspace_leases.pop(session.workspace_id, None)
        await self._drain_session_queue(session)
        return True

    async def _drain_session_queue(self, session: Session) -> None:
        queued = sorted(
            (
                item
                for item in self.conversations.values()
                if item.run.state == ExecutionState.QUEUED
            ),
            key=lambda item: item.created_at,
        )
        for conversation in queued:
            candidate = self.sessions.get(conversation.session_id)
            if candidate is None and self.repository:
                candidate = self.repository.get_session(conversation.session_id)
                if candidate:
                    self.sessions[candidate.id] = candidate
            if candidate is None or candidate.active_run_id:
                continue
            if not await self._acquire_container(candidate):
                continue
            candidate.active_run_id = conversation.run.run_id
            if self.repository:
                self.repository.save_session(candidate)
            conversation.run.state = ExecutionState.STARTING
            self._append(conversation, "run.started", {"session_id": str(candidate.id)})
            conversation.run.state = ExecutionState.RUNNING
            self._append(
                conversation, "run.running", {"container_id": candidate.active_container_id}
            )
            if self.core_runtime:
                await self._run_core(conversation, candidate)
            return

    def _conversation(self, conversation_id: UUID) -> Conversation:
        conversation = None if self.distributed else self.conversations.get(conversation_id)
        if not conversation and self.repository:
            conversation = self.repository.get_conversation(conversation_id)
            if conversation and not self.distributed:
                self.conversations[conversation.id] = conversation
                for event in self.repository.list_events(conversation.id):
                    if not self.events_store.list(conversation.id, event.seq - 1):
                        self.events_store.append(conversation.id, event)
        if not conversation:
            raise ServiceError("CONVERSATION_NOT_FOUND", "conversation does not exist", 404)
        return conversation

    def _checkpoint(self, conversation: Conversation) -> Checkpoint | None:
        checkpoint_id = conversation.run.checkpoint_id
        if checkpoint_id is None:
            return None
        checkpoint = self.checkpoints.get(checkpoint_id)
        if checkpoint is None and self.repository:
            checkpoint = self.repository.get_checkpoint(checkpoint_id)
            if checkpoint:
                self.checkpoints[checkpoint.checkpoint_id] = checkpoint
        return checkpoint

    def request_interaction(
        self, conversation_id: UUID, interaction: dict[str, Any]
    ) -> Conversation:
        conversation = self._conversation(conversation_id)
        if conversation.run.state != ExecutionState.RUNNING:
            raise ServiceError("INVALID_STATE", "interaction requires RUNNING conversation", 409)
        conversation.run.pending_interaction = interaction
        conversation.run.state = ExecutionState.WAITING_INPUT
        self._append(conversation, "interaction.requested", interaction)
        return conversation

    async def submit_input(
        self,
        conversation_id: UUID,
        interaction_id: str,
        value: Any,
        expected_seq: int | None = None,
        idempotency_key: str | None = None,
    ) -> Conversation:
        conversation = self._conversation(conversation_id)
        payload = {
            "conversation_id": conversation_id,
            "interaction_id": interaction_id,
            "value": value,
            "expected_seq": expected_seq,
        }
        if self.distributed:
            assert self.repository is not None
            try:
                _, persisted = self.repository.enqueue_conversation_command(
                    conversation_id,
                    "input",
                    {"interaction_id": interaction_id, "value": value},
                    idempotency_key or uuid4().hex,
                    expected_seq=expected_seq,
                    interaction_id=interaction_id,
                )
            except RepositoryConflict as exc:
                raise ServiceError("COMMAND_CONFLICT", str(exc), 409) from exc
            return persisted
        existing = self._idempotent("input", idempotency_key, payload)
        if existing:
            return self._conversation(existing)
        if self.repository and idempotency_key:
            try:
                existing = self.repository.find_idempotent(
                    "input", idempotency_key, _hash_request(payload)
                )
            except RepositoryConflict as exc:
                raise ServiceError("IDEMPOTENCY_CONFLICT", str(exc), 409) from exc
            if existing:
                self._remember("input", idempotency_key, payload, existing)
                return self._conversation(existing)
        self._check_expected_seq(conversation, expected_seq)
        self._check_interaction(conversation, interaction_id)
        was_paused = conversation.run.state == ExecutionState.PAUSED
        checkpoint = self._checkpoint(conversation) if was_paused else None
        if was_paused:
            if checkpoint is None:
                raise ServiceError(
                    "CHECKPOINT_NOT_FOUND", "conversation checkpoint is missing", 409
                )
            try:
                validate_checkpoint(
                    checkpoint,
                    lease_epoch=self.sessions[conversation.session_id].lease_epoch,
                    expected_tool_policy_hash=tool_policy_hash(
                        self._tool_policy_for_session(self.sessions[conversation.session_id])
                    ),
                )
            except ValueError as exc:
                raise ServiceError("CHECKPOINT_INVALID", str(exc), 409) from exc
        if conversation.run.state == ExecutionState.PAUSED:
            await self._resume_session(conversation)
        conversation.run.pending_interaction = None
        conversation.run.state = ExecutionState.RUNNING
        self._append(
            conversation, "interaction.input", {"interaction_id": interaction_id, "value": value}
        )
        if self.core_runtime:
            try:
                await self._register_core_endpoint(
                    self.sessions[conversation.session_id], conversation.run.run_id
                )
                if was_paused:

                    async def sink(event: EventEnvelope) -> None:
                        self._apply_core_event(conversation, event)

                    response = await self.core_runtime.resume(checkpoint, value, sink)
                    response = {**response, "events": []}
                else:
                    response = await self.core_runtime.accept_input(
                        conversation.run.run_id, interaction_id, value
                    )
                await self._forward_core_events(conversation, response)
            except ServiceError:
                raise
            except Exception as exc:  # noqa: BLE001 - runtime errors become run failures
                conversation.run.state = ExecutionState.FAILED
                self._append(
                    conversation,
                    "run.failed",
                    {"code": "CORE_RUNTIME_ERROR", "message": str(exc)},
                )
        if conversation.run.state in TERMINAL_STATES:
            await self._release_session(self.sessions[conversation.session_id], conversation)
        self._remember_persisted(
            "input", idempotency_key, payload, conversation.id, conversation.model_dump(mode="json")
        )
        return conversation

    async def submit_approval(
        self,
        conversation_id: UUID,
        approval_id: str,
        decision: str,
        expected_seq: int | None = None,
        idempotency_key: str | None = None,
    ) -> Conversation:
        if decision not in {"APPROVE_ONCE", "REJECT"}:
            raise ServiceError("INVALID_DECISION", "decision must be APPROVE_ONCE or REJECT", 422)
        conversation = self._conversation(conversation_id)
        payload = {
            "conversation_id": conversation_id,
            "approval_id": approval_id,
            "decision": decision,
            "expected_seq": expected_seq,
        }
        if self.distributed:
            assert self.repository is not None
            try:
                _, persisted = self.repository.enqueue_conversation_command(
                    conversation_id,
                    "approval",
                    {"approval_id": approval_id, "decision": decision},
                    idempotency_key or uuid4().hex,
                    expected_seq=expected_seq,
                    interaction_id=approval_id,
                )
            except RepositoryConflict as exc:
                raise ServiceError("COMMAND_CONFLICT", str(exc), 409) from exc
            return persisted
        existing = self._idempotent("approval", idempotency_key, payload)
        if existing:
            return self._conversation(existing)
        if self.repository and idempotency_key:
            try:
                existing = self.repository.find_idempotent(
                    "approval", idempotency_key, _hash_request(payload)
                )
            except RepositoryConflict as exc:
                raise ServiceError("IDEMPOTENCY_CONFLICT", str(exc), 409) from exc
            if existing:
                self._remember("approval", idempotency_key, payload, existing)
                return self._conversation(existing)
        self._check_expected_seq(conversation, expected_seq)
        self._check_interaction(conversation, approval_id)
        was_paused = conversation.run.state == ExecutionState.PAUSED
        checkpoint = self._checkpoint(conversation) if was_paused else None
        if was_paused:
            if checkpoint is None:
                raise ServiceError(
                    "CHECKPOINT_NOT_FOUND", "conversation checkpoint is missing", 409
                )
            try:
                validate_checkpoint(
                    checkpoint,
                    lease_epoch=self.sessions[conversation.session_id].lease_epoch,
                    expected_tool_policy_hash=tool_policy_hash(
                        self._tool_policy_for_session(self.sessions[conversation.session_id])
                    ),
                )
            except ValueError as exc:
                raise ServiceError("CHECKPOINT_INVALID", str(exc), 409) from exc
        if conversation.run.state == ExecutionState.PAUSED:
            await self._resume_session(conversation)
        conversation.run.pending_interaction = None
        conversation.run.state = ExecutionState.RUNNING
        self._append(
            conversation, "approval.decided", {"approval_id": approval_id, "decision": decision}
        )
        if self.core_runtime:
            try:
                await self._register_core_endpoint(
                    self.sessions[conversation.session_id], conversation.run.run_id
                )
                if was_paused:

                    async def sink(event: EventEnvelope) -> None:
                        self._apply_core_event(conversation, event)

                    response = await self.core_runtime.resume(checkpoint, decision, sink)
                    response = {**response, "events": []}
                else:
                    response = await self.core_runtime.accept_approval(
                        conversation.run.run_id, approval_id, decision
                    )
                await self._forward_core_events(conversation, response)
            except ServiceError:
                raise
            except Exception as exc:  # noqa: BLE001 - runtime errors become run failures
                conversation.run.state = ExecutionState.FAILED
                self._append(
                    conversation,
                    "run.failed",
                    {"code": "CORE_RUNTIME_ERROR", "message": str(exc)},
                )
        if conversation.run.state in TERMINAL_STATES:
            await self._release_session(self.sessions[conversation.session_id], conversation)
        self._remember_persisted(
            "approval",
            idempotency_key,
            payload,
            conversation.id,
            conversation.model_dump(mode="json"),
        )
        return conversation

    def _check_expected_seq(self, conversation: Conversation, expected_seq: int | None) -> None:
        if expected_seq is not None and expected_seq != conversation.run.last_seq:
            raise ServiceError("CONFLICT", "expected_seq does not match conversation", 409)

    def _check_interaction(self, conversation: Conversation, interaction_id: str) -> None:
        pending = conversation.run.pending_interaction
        if (
            conversation.run.state not in {ExecutionState.WAITING_INPUT, ExecutionState.PAUSED}
            or not pending
        ):
            raise ServiceError("INVALID_STATE", "conversation is not waiting for input", 409)
        if pending.get("interaction_id") != interaction_id:
            raise ServiceError(
                "INTERACTION_NOT_FOUND", "interaction does not match pending interaction", 409
            )

    async def pause(
        self, conversation_id: UUID, reason: str = "waiting_input_timeout"
    ) -> Conversation:
        conversation = self._conversation(conversation_id)
        if conversation.run.state != ExecutionState.WAITING_INPUT:
            raise ServiceError("INVALID_STATE", "only WAITING_INPUT can be paused", 409)
        conversation.run.state = ExecutionState.SUSPENDING
        self._append(conversation, "checkpoint.requested", {"reason": reason})
        if self.core_runtime:
            await self.core_runtime.checkpoint(conversation.run.run_id, reason)
        self.create_checkpoint(conversation_id, reason)
        session = self.sessions[conversation.session_id]
        if not await self._release_session(session, conversation):
            return conversation
        conversation.run.state = ExecutionState.PAUSED
        self._append(conversation, "run.paused", {"reason": reason})
        return conversation

    async def pause_expired_waiting(self, now: datetime | None = None) -> int:
        """Pause WAITING_INPUT conversations whose last interaction has expired."""

        current_time = now or datetime.now(UTC)
        paused = 0
        for conversation in list(self.conversations.values()):
            if conversation.run.state != ExecutionState.WAITING_INPUT:
                continue
            requested = [
                event.occurred_at
                for event in self.events_store.list(conversation.id)
                if event.type == "interaction.requested"
            ]
            if not requested:
                continue
            if (
                current_time - max(requested)
            ).total_seconds() < self.config.waiting_input_timeout_seconds:
                continue
            await self.pause(conversation.id, reason="waiting_input_timeout")
            paused += 1
        return paused

    def prune_retained_state(self, now: datetime | None = None) -> dict[str, int]:
        """Drop transient in-memory records that outlived their retention windows.

        Only idempotency records and checkpoints that are no longer referenced
        by any conversation are pruned. Workspaces, sessions, conversations and
        events are authoritative data and are intentionally left untouched by
        this pass; PostgreSQL-side retention is handled by the dedicated
        retention process.
        """

        current = now or datetime.now(UTC)
        idempotency_cutoff = current - timedelta(
            hours=self.config.retention_idempotency_hours
        )
        stale_idempotency = [
            key
            for key, record in self.idempotency.items()
            if record.created_at < idempotency_cutoff
        ]
        for key in stale_idempotency:
            del self.idempotency[key]
        referenced = {
            conversation.run.checkpoint_id
            for conversation in self.conversations.values()
            if conversation.run.checkpoint_id is not None
        }
        checkpoint_cutoff = current - timedelta(
            hours=self.config.retention_unreferenced_checkpoints_hours
        )
        stale_checkpoints = [
            checkpoint_id
            for checkpoint_id, checkpoint in self.checkpoints.items()
            if checkpoint_id not in referenced and checkpoint.created_at < checkpoint_cutoff
        ]
        for checkpoint_id in stale_checkpoints:
            del self.checkpoints[checkpoint_id]
        return {
            "idempotency_records": len(stale_idempotency),
            "unreferenced_checkpoints": len(stale_checkpoints),
        }

    def register_runner(
        self, request: RunnerRegistrationRequest, *, bootstrap_token: str | None
    ) -> RunnerRegistrationResponse:
        if not self.config.runner_token:
            raise ServiceError(
                "RUNNER_REGISTRATION_DISABLED",
                "runner registration is not configured; set AGENTSUPPORT_RUNNER_TOKEN",
                503,
            )
        if not bootstrap_token or not secrets.compare_digest(
            bootstrap_token, self.config.runner_token
        ):
            raise ServiceError("RUNNER_TOKEN_INVALID", "invalid runner token", 401)
        runner_token = secrets.token_urlsafe(32)
        try:
            entry = self.runner_registry.register(request, _hash_token(runner_token))
        except RunnerRegistryConflict as exc:
            raise ServiceError("RUNNER_ALREADY_REGISTERED", str(exc), 409) from exc
        return RunnerRegistrationResponse(
            runner_id=entry.runner_id, token=runner_token
        )

    def _verify_runner_token(self, runner_id: UUID, runner_token: str | None) -> None:
        if not runner_token:
            raise ServiceError("RUNNER_TOKEN_INVALID", "missing runner token", 401)
        stored = self.runner_registry.token_hash(runner_id)
        if stored is None:
            raise ServiceError("RUNNER_NOT_FOUND", "runner is not registered", 404)
        if not secrets.compare_digest(_hash_token(runner_token), stored):
            raise ServiceError("RUNNER_TOKEN_INVALID", "invalid runner token", 401)

    def runner_heartbeat(
        self, runner_id: UUID, payload: RunnerHeartbeat, *, runner_token: str | None
    ) -> RunnerRegistration:
        self._verify_runner_token(runner_id, runner_token)
        updated = self.runner_registry.heartbeat(
            runner_id,
            payload.status,
            payload.load,
            capabilities=payload.capabilities,
        )
        if updated is None:
            raise ServiceError("RUNNER_NOT_FOUND", "runner is not registered", 404)
        return updated

    def runner_deregister(self, runner_id: UUID, *, runner_token: str | None) -> None:
        self._verify_runner_token(runner_id, runner_token)
        if not self.runner_registry.deregister(runner_id):
            raise ServiceError("RUNNER_NOT_FOUND", "runner is not registered", 404)

    def runner_snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "runner_id": str(entry.runner_id),
                "type": entry.provider,
                "version": entry.version,
                "capabilities": list(entry.capabilities),
                "status": entry.status,
            }
            for entry in self.runner_registry.list_ready()
        ]

    def select_ready_runner(self, capabilities: set[str]) -> RunnerRegistration | None:
        ready = self.runner_registry.list_ready(capabilities)
        return ready[0] if ready else None

    def prune_stale_runners(self) -> int:
        expired = self.runner_registry.expire_stale(
            at=datetime.now(UTC),
            timeout_seconds=self.config.runner_heartbeat_timeout_seconds,
        )
        if expired:
            obs_metrics.record_runner_heartbeat_expired()
        return len(expired)

    async def supervise_active_sessions(self) -> int:
        """Mark sessions LOST only after repeated failed container health checks."""

        lost = 0
        for session in list(self.sessions.values()):
            if not session.active_container_id:
                self._health_failures.pop(session.id, None)
                continue
            try:
                inspection = await self.runtime_driver.inspect(session.active_container_id)
                healthy = (
                    inspection.get("status") in {"running", "created"}
                    and session.active_run_id is not None
                )
                if healthy and self.core_runtime is not None and session.active_run_id is not None:
                    endpoint = await self._runner_endpoint(session)
                    register = getattr(self.core_runtime, "register_run_endpoint", None)
                    if register is not None:
                        register(session.active_run_id, endpoint)
                    health = await self.core_runtime.health(session.active_run_id)
                    healthy = health.get("live", {}).get("status") == "ok" and health.get(
                        "ready", {}
                    ).get("status") in {"ok", "ready"}
            except Exception:  # noqa: BLE001 - health failures are counted, not raised
                healthy = False
            if healthy:
                self._health_failures.pop(session.id, None)
                continue
            failures = self._health_failures.get(session.id, 0) + 1
            self._health_failures[session.id] = failures
            if failures < self.config.health_failure_threshold:
                continue
            if session.active_run_id is None:
                if await self._release_session(session):
                    self._health_failures.pop(session.id, None)
                    lost += 1
                continue
            conversation = next(
                (
                    item
                    for item in self.conversations.values()
                    if item.run.run_id == session.active_run_id
                ),
                None,
            )
            if conversation is None:
                continue
            conversation.run.state = ExecutionState.LOST
            self._append(
                conversation,
                "run.lost",
                {"container_id": session.active_container_id, "health_failures": failures},
            )
            await self._release_session(session, conversation)
            self._health_failures.pop(session.id, None)
            lost += 1
        return lost

    def create_checkpoint(self, conversation_id: UUID, reason: str) -> Checkpoint:
        conversation = self._conversation(conversation_id)
        session = self.sessions[conversation.session_id]
        pending_interaction = conversation.run.pending_interaction
        pending_tool_calls: list[dict[str, Any]] = []
        tool_batch_hash: str | None = None
        if pending_interaction:
            raw_batch = pending_interaction.get("tool_batch")
            if isinstance(raw_batch, dict):
                try:
                    batch = ToolBatch.model_validate(raw_batch)
                except Exception as exc:
                    raise ServiceError(
                        "CHECKPOINT_INVALID", "pending tool batch is invalid", 409
                    ) from exc
                pending_tool_calls = [call.model_dump(mode="json") for call in batch.calls]
                tool_batch_hash = batch.batch_hash
            else:
                pending_tool_calls = list(pending_interaction.get("pending_tool_calls", []))
                tool_batch_hash = pending_interaction.get("tool_batch_hash")
        raw_tool_policy = pending_interaction.get("tool_policy", {}) if pending_interaction else {}
        tool_policy: dict[str, Any] = self._tool_policy_for_session(session)
        tool_policy.update(raw_tool_policy)
        tool_policy["reason"] = reason
        if pending_tool_calls:
            tool_policy["pending_tool_calls"] = pending_tool_calls
        if tool_batch_hash:
            tool_policy["tool_batch_hash"] = tool_batch_hash
        checkpoint_tool_policy_hash = tool_policy_hash(tool_policy)
        raw_tools = list(tool_policy.get("tools", []))
        checkpoint_tool_versions_hash = tool_versions_hash(raw_tools) if raw_tools else None
        tool_policy["tool_policy_hash"] = checkpoint_tool_policy_hash
        if checkpoint_tool_versions_hash:
            tool_policy["tool_versions_hash"] = checkpoint_tool_versions_hash
        context = ContextBundle(
            task=conversation.task,
            conversation_id=conversation.id,
            workspace_ref="/workspace",
            recent_events=[
                event.model_dump(mode="json") for event in self.events_store.list(conversation.id)
            ],
            tool_policy=tool_policy,
        )
        context_hash = hashlib.sha256(
            json.dumps(
                context.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        checkpoint = Checkpoint(
            conversation_id=conversation.id,
            run_id=conversation.run.run_id,
            last_event_seq=conversation.run.last_seq,
            context_bundle=context,
            pending_interaction=pending_interaction,
            pending_tool_calls=pending_tool_calls,
            tool_batch_hash=tool_batch_hash,
            tool_policy_hash=checkpoint_tool_policy_hash,
            tool_versions_hash=checkpoint_tool_versions_hash,
            workspace_ref="/workspace",
            workspace_write_lease_epoch=session.lease_epoch,
            context_bundle_hash=context_hash,
        )
        self.checkpoints[checkpoint.checkpoint_id] = checkpoint
        conversation.run.checkpoint_id = checkpoint.checkpoint_id
        event = EventEnvelope(
            run_id=conversation.run.run_id,
            seq=conversation.run.last_seq + 1,
            type="checkpoint.created",
            payload={"checkpoint_id": str(checkpoint.checkpoint_id)},
            source="agentsupport",
        )
        if self.repository:
            try:
                self.repository.save_checkpoint_and_append_event(checkpoint, conversation, event)
            except RepositoryConflict as exc:
                raise ServiceError("CHECKPOINT_CONFLICT", str(exc), 409) from exc
        self.events_store.append(conversation.id, event)
        conversation.run.last_seq = event.seq
        try:
            asyncio.get_running_loop().create_task(
                self.events_store.publish(conversation.id, event)
            )
        except RuntimeError:
            pass
        return checkpoint

    async def _resume_session(self, conversation: Conversation) -> None:
        session = self.sessions[conversation.session_id]
        if not session.active_container_id and not await self._acquire_container(session):
            raise ServiceError("RESOURCE_EXHAUSTED", "session container capacity is exhausted", 429)
        session.active_run_id = conversation.run.run_id
        await self._register_core_endpoint(session, conversation.run.run_id)
        if self.repository:
            self.repository.save_session(session)

    async def cancel(
        self,
        conversation_id: UUID,
        expected_seq: int | None = None,
        idempotency_key: str | None = None,
    ) -> Conversation:
        conversation = self._conversation(conversation_id)
        payload = {"conversation_id": conversation_id, "expected_seq": expected_seq}
        if self.distributed:
            assert self.repository is not None
            try:
                _, persisted = self.repository.enqueue_conversation_command(
                    conversation_id,
                    "cancel",
                    {},
                    idempotency_key or uuid4().hex,
                    expected_seq=expected_seq,
                )
            except RepositoryConflict as exc:
                raise ServiceError("COMMAND_CONFLICT", str(exc), 409) from exc
            return persisted
        existing = self._idempotent("cancel", idempotency_key, payload)
        if existing:
            return self._conversation(existing)
        if self.repository and idempotency_key:
            try:
                existing = self.repository.find_idempotent(
                    "cancel", idempotency_key, _hash_request(payload)
                )
            except RepositoryConflict as exc:
                raise ServiceError("IDEMPOTENCY_CONFLICT", str(exc), 409) from exc
            if existing:
                self._remember("cancel", idempotency_key, payload, existing)
                return self._conversation(existing)
        if conversation.run.state in TERMINAL_STATES:
            self._remember_persisted(
                "cancel",
                idempotency_key,
                payload,
                conversation.id,
                conversation.model_dump(mode="json"),
            )
            return conversation
        self._check_expected_seq(conversation, expected_seq)
        if self.core_runtime and conversation.run.state in {
            ExecutionState.RUNNING,
            ExecutionState.WAITING_INPUT,
        }:
            try:
                await self._register_core_endpoint(
                    self.sessions[conversation.session_id], conversation.run.run_id
                )
                response = await self.core_runtime.cancel(conversation.run.run_id)
                await self._forward_core_events(conversation, response)
            except Exception as exc:  # noqa: BLE001 - runtime errors become run failures
                conversation.run.state = ExecutionState.FAILED
                self._append(
                    conversation,
                    "run.failed",
                    {"code": "CORE_RUNTIME_ERROR", "message": str(exc)},
                )
        if conversation.run.state not in TERMINAL_STATES:
            conversation.run.pending_interaction = None
            conversation.run.state = ExecutionState.CANCELLED
            self._append(conversation, "run.cancelled", {})
        session = self.sessions[conversation.session_id]
        if session.active_run_id == conversation.run.run_id:
            await self._release_session(session, conversation)
        self._remember_persisted(
            "cancel",
            idempotency_key,
            payload,
            conversation.id,
            conversation.model_dump(mode="json"),
        )
        return conversation

    def events(self, conversation_id: UUID, after_seq: int = 0) -> list[EventEnvelope]:
        conversation = self._conversation(conversation_id)
        if self.repository:
            raw = self.repository.list_events(conversation_id, after_seq)
        else:
            raw = self.events_store.list(conversation_id, after_seq)
        tenant_id, user_id, project_id = self._session_labels(conversation.session_id)
        return [
            event.model_copy(
                update={
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "project_id": project_id,
                }
            )
            for event in raw
        ]

    async def stream_events(
        self, conversation_id: UUID, after_seq: int = 0
    ) -> AsyncIterator[EventEnvelope]:
        self._conversation(conversation_id)
        if not self.distributed:
            async for event in self.events_store.stream(conversation_id, after_seq):
                yield event
            return
        cursor = after_seq
        while True:
            events = self.events(conversation_id, cursor)
            if events:
                for event in events:
                    cursor = event.seq
                    yield event
                continue
            try:
                await self.event_notifier.wait(
                    "conversation.events",
                    conversation_id,
                    self.config.event_poll_interval_seconds,
                )
            except Exception:  # noqa: BLE001 - database polling remains authoritative
                await asyncio.sleep(self.config.event_poll_interval_seconds)

    def session_events(self, session_id: UUID, after_seq: int = 0) -> list[EventEnvelope]:
        session = (
            self.repository.get_session(session_id)
            if self.distributed and self.repository
            else self.sessions.get(session_id)
        )
        if session is None:
            raise ServiceError("SESSION_NOT_FOUND", "session does not exist", 404)
        conversations = (
            self.repository.list_conversations()
            if self.distributed and self.repository
            else list(self.conversations.values())
        )
        events = [
            event
            for conversation in conversations
            if conversation.session_id == session_id
            for event in self.events(conversation.id, after_seq)
        ]
        return sorted(events, key=lambda event: event.occurred_at)

    async def stream_session_events(
        self, session_id: UUID, after_seq: int = 0
    ) -> AsyncIterator[EventEnvelope]:
        self.get_session(session_id)
        cursors: dict[UUID, int] = {}
        while True:
            conversations = (
                self.repository.list_conversations()
                if self.distributed and self.repository
                else list(self.conversations.values())
            )
            for conversation in conversations:
                if conversation.session_id != session_id:
                    continue
                cursor = cursors.get(conversation.id, after_seq)
                for event in self.events(conversation.id, cursor):
                    cursors[conversation.id] = event.seq
                    yield event
            try:
                await self.event_notifier.wait(
                    "session.events",
                    session_id,
                    self.config.event_poll_interval_seconds,
                )
            except Exception:  # noqa: BLE001 - polling remains authoritative
                await asyncio.sleep(self.config.event_poll_interval_seconds)

    def readiness(self) -> dict[str, Any]:
        if self.repository:
            self.repository.health_check()
        return {
            "status": "ready",
            "execution_mode": self.config.execution_mode,
            "persistence_mode": self.config.persistence_mode,
            "instance_id": self.instance_id,
        }

    def metrics(self) -> dict[str, int]:
        if self.repository:
            return self.repository.coordination_metrics()
        return {
            "queue_ready": sum(
                item.run.state == ExecutionState.QUEUED
                for item in self.conversations.values()
            ),
            "queue_oldest_ready_seconds": 0,
            "jobs_claimed": 0,
            "jobs_running": sum(
                item.run.state == ExecutionState.RUNNING
                for item in self.conversations.values()
            ),
            "jobs_waiting": sum(
                item.run.state == ExecutionState.WAITING_INPUT
                for item in self.conversations.values()
            ),
            "jobs_paused": sum(
                item.run.state == ExecutionState.PAUSED
                for item in self.conversations.values()
            ),
            "claims_expired": 0,
            "active_runtimes": sum(
                item.active_container_id is not None for item in self.sessions.values()
            ),
            "runner_starting": 0,
            "runner_reconciliation_needed": 0,
            "workspace_lease_contention": 0,
            "outbox_pending": 0,
            "outbox_publication_lag_seconds": 0,
        }

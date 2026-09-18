"""Skill and MCP server operations."""



from __future__ import annotations

from typing import Any

from ..domain import (
    SUPPORTED_TRANSPORTS,
    McpServer,
    Session,
)
from .common import (
    _MCP_SERVER_ID,
    ServiceError,
)


class SkillMcpOpsMixin:

    def list_skills(self, *, tenant_id: str | None = None) -> list[dict[str, Any]]:
        """Skills a namespace can use: its own tenant directory, then the shared one."""

        return self.skill_provider.list_skills(tenant_id=tenant_id)


    def get_skill(self, skill_id: str, *, tenant_id: str | None = None) -> dict[str, Any]:
        try:
            return self.skill_provider.describe_skill(skill_id, tenant_id=tenant_id)
        except (FileNotFoundError, ValueError) as exc:
            raise ServiceError("SKILL_NOT_FOUND", str(exc), 404) from exc


    def create_skill(
        self,
        skill_id: str,
        *,
        filename: str,
        payload: bytes,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Install a skill into the tenant namespace (or the shared one when None)."""

        try:
            if filename.lower().endswith(".zip"):
                return self.skill_provider.install_zip(
                    skill_id, payload, tenant_id=tenant_id
                )
            return self.skill_provider.install_skill(
                skill_id, {"SKILL.md": payload}, tenant_id=tenant_id
            )
        except ValueError as exc:
            raise ServiceError("SKILL_INVALID_PAYLOAD", str(exc), 422) from exc


    def delete_skill(self, skill_id: str, *, tenant_id: str | None = None) -> None:
        try:
            removed = self.skill_provider.remove_skill(skill_id, tenant_id=tenant_id)
        except ValueError as exc:
            raise ServiceError("SKILL_INVALID_PAYLOAD", str(exc), 422) from exc
        if not removed:
            raise ServiceError("SKILL_NOT_FOUND", "skill does not exist", 404)


    def _validate_skill_ids(
        self, skill_ids: list[str], *, tenant_id: str | None = None
    ) -> None:
        if not skill_ids:
            return
        try:
            self.skill_provider.resolve(skill_ids, tenant_id=tenant_id)
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
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
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
        if transport == "stdio" and not command:
            raise ServiceError(
                "MCP_SERVER_INVALID", "stdio transport requires command", 422
            )
        server = McpServer(
            server_id=server_id,
            name=name,
            transport=transport,
            http_url=http_url,
            sse_url=sse_url,
            headers=dict(headers or {}),
            command=command,
            args=list(args or []),
            env=dict(env or {}),
            cwd=cwd,
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
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
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
                "command": command if command is not None else current.command,
                "args": list(args) if args is not None else list(current.args),
                "env": dict(env) if env is not None else dict(current.env),
                "cwd": cwd if cwd is not None else current.cwd,
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
                    #: stdio 专用：runner 用这些字段把 MCP 服务拉成子进程
                    "command": server.command,
                    "args": list(server.args),
                    "env": dict(server.env),
                    "cwd": server.cwd,
                    "description": server.description,
                }
            )
        return resolved



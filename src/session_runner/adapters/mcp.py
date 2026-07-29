from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from agent_runner_contracts.tools import ToolDescriptor

McpHandler = Callable[[dict[str, Any]], Awaitable[Any]]


class ControlledMcpProvider:
    """Runner-side MCP adapter that exposes only explicitly authorized servers."""

    def __init__(
        self,
        tools: dict[tuple[str, str], McpHandler],
        *,
        allowed_servers: set[str],
        approval_required: bool = True,
    ) -> None:
        self._tools = tools
        self.allowed_servers = allowed_servers
        self.approval_required = approval_required

    def _validate_refs(self, server_refs: list[str]) -> None:
        denied = sorted(set(server_refs) - self.allowed_servers)
        if denied:
            raise ValueError(f"MCP servers are not authorized: {denied}")

    def descriptors(self, server_refs: list[str]) -> list[ToolDescriptor]:
        self._validate_refs(server_refs)
        return [
            ToolDescriptor(
                name=f"mcp.{server}.{tool}",
                requires_approval=self.approval_required,
                description=f"MCP tool {server}/{tool}",
            )
            for server, tool in self._tools
            if server in server_refs
        ]

    def handlers(self, server_refs: list[str]) -> dict[str, McpHandler]:
        self._validate_refs(server_refs)
        return {
            f"mcp.{server}.{tool}": handler
            for (server, tool), handler in self._tools.items()
            if server in server_refs
        }

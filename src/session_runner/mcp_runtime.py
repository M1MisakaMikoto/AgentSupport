"""Per-run MCP wiring: config resolution and gateway-path connections.

Trae mode uses the vendored agent's own MCP client machinery; this module
provides the resolved per-run server config for that path and a
``ControlledMcpProvider``-compatible connection builder for the tool-gateway
path (deterministic / raw-batch executor).
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

from .adapters.mcp import ControlledMcpProvider

logger = logging.getLogger(__name__)


def _resolve_header_value(value: str) -> str:
    if value.startswith("$"):
        name = value[1:]
        resolved = os.getenv(name)
        if resolved is None:
            raise RuntimeError(f"MCP header env var is not set: {name}")
        return resolved
    return value


def _resolve_headers(raw: dict[str, str] | None) -> dict[str, str]:
    return {key: _resolve_header_value(value) for key, value in (raw or {}).items()}


def _resolve_env_values(raw: dict[str, str] | None) -> dict[str, str]:
    """stdio 的 env 与 headers 用同一套 ``$VAR`` 占位规则（密钥不写进注册表）。"""
    return _resolve_headers(raw)


def build_mcp_server_configs(
    mcp_refs: list[dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """Convert resolved references into vendored ``MCPServerConfig``-shaped dicts.

    三种 transport 都要能表达，尤其是 **stdio**：trae 模式的 vendored 客户端只有
    stdio 实现（http/url 是 ``NotImplementedError``，而且被静默吞掉），
    所以"按会话下发 MCP"这条路必须先支持 stdio。
    """

    configs: dict[str, dict[str, Any]] = {}
    for ref in mcp_refs or []:
        if not isinstance(ref, dict):
            continue
        server_id = str(ref.get("server_id", ""))
        if not server_id:
            continue
        transport = ref.get("transport")
        headers = _resolve_headers(ref.get("headers"))
        if transport == "http":
            configs[server_id] = {"http_url": ref.get("http_url"), "headers": headers}
        elif transport == "sse":
            configs[server_id] = {"url": ref.get("sse_url"), "headers": headers}
        elif transport == "stdio":
            configs[server_id] = {
                "command": ref.get("command"),
                "args": list(ref.get("args") or []),
                "env": _resolve_env_values(ref.get("env")),
                "cwd": ref.get("cwd"),
            }
    return configs


class _McpConnection:
    def __init__(self, session: ClientSession, client_ctx: Any) -> None:
        self.session = session
        self._client_ctx = client_ctx

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.session.__aexit__(None, None, None)
        with contextlib.suppress(Exception):
            await self._client_ctx.__aexit__(None, None, None)


class McpRuntimeProvider(ControlledMcpProvider):
    """Gateway-path MCP provider owning live server connections."""

    def __init__(
        self,
        tools: dict[tuple[str, str], Any],
        *,
        allowed_servers: set[str],
        connections: list[_McpConnection],
    ) -> None:
        super().__init__(tools, allowed_servers=allowed_servers)
        self._connections = connections

    async def close(self) -> None:
        for connection in self._connections:
            with contextlib.suppress(Exception):
                await connection.close()
        self._connections = []


def _make_handler(session: ClientSession, tool_name: str) -> Any:
    async def handler(arguments: dict[str, Any]) -> Any:
        result = await session.call_tool(tool_name, arguments)
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return structured
        parts = []
        for item in result.content or []:
            text = getattr(item, "text", None)
            if text is not None:
                parts.append(text)
        return {"content": parts}

    return handler


async def build_mcp_provider(
    mcp_refs: list[dict[str, Any]] | None,
) -> McpRuntimeProvider | None:
    """Connect authorized servers and expose their tools for the tool gateway."""

    tools: dict[tuple[str, str], Any] = {}
    allowed: set[str] = set()
    connections: list[_McpConnection] = []
    try:
        for ref in mcp_refs or []:
            if not isinstance(ref, dict):
                continue
            server_id = str(ref.get("server_id", ""))
            transport = ref.get("transport")
            headers = _resolve_headers(ref.get("headers"))
            if transport == "http":
                url = ref.get("http_url")
                client_ctx = streamablehttp_client(url, headers=headers)
                read, write, _ = await client_ctx.__aenter__()
            elif transport == "sse":
                url = ref.get("sse_url")
                client_ctx = sse_client(url, headers=headers)
                read, write = await client_ctx.__aenter__()
            else:
                continue
            session = ClientSession(read, write)
            await session.__aenter__()
            await session.initialize()
            connections.append(_McpConnection(session, client_ctx))
            listed = await session.list_tools()
            for tool in listed.tools:
                tools[(server_id, tool.name)] = _make_handler(session, tool.name)
            allowed.add(server_id)
            logger.info(
                "connected MCP server %s with %d tools", server_id, len(listed.tools)
            )
        if not tools:
            return None
        return McpRuntimeProvider(tools, allowed_servers=allowed, connections=connections)
    except Exception:
        for connection in connections:
            with contextlib.suppress(Exception):
                await connection.close()
        raise


__all__ = [
    "McpRuntimeProvider",
    "build_mcp_provider",
    "build_mcp_server_configs",
]

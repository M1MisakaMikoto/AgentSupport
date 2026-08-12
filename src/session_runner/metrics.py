"""Prometheus metrics for the Session Runner."""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    generate_latest,
)

REGISTRY = CollectorRegistry(auto_describe=False)

http_requests = Counter(
    "session_runner_http_requests_total",
    "HTTP requests handled by the runner",
    ["method", "route", "status"],
    registry=REGISTRY,
)
mcp_connections = Counter(
    "session_runner_mcp_connections_total",
    "MCP server connection attempts",
    ["result"],
    registry=REGISTRY,
)


def record_http(method: str, route: str, status: int) -> None:
    http_requests.labels(method=method, route=route, status=str(status)).inc()


def record_mcp_connection(result: str) -> None:
    mcp_connections.labels(result=result).inc()


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


__all__ = ["record_http", "record_mcp_connection", "render_metrics"]

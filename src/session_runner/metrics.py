"""Prometheus metrics for the Session Runner."""

from __future__ import annotations

from typing import Any

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
llm_tokens = Counter(
    "session_runner_llm_tokens_total",
    "Model tokens consumed by the runner",
    ["kind"],
    registry=REGISTRY,
)


def record_http(method: str, route: str, status: int) -> None:
    http_requests.labels(method=method, route=route, status=str(status)).inc()


def record_mcp_connection(result: str) -> None:
    mcp_connections.labels(result=result).inc()


def record_llm_usage(usage: dict[str, Any]) -> None:
    """Record a segment's model usage as per-kind Prometheus counters."""

    for kind in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "reasoning_tokens",
    ):
        value = int(usage.get(kind) or 0)
        if value > 0:
            llm_tokens.labels(kind=kind).inc(value)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


__all__ = ["record_http", "record_llm_usage", "record_mcp_connection", "render_metrics"]

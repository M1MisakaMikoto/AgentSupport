"""Context propagation for correlation IDs and run identity across async work."""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator

CORRELATION_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)
TRACE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trace_id", default=None
)
SPAN_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "span_id", default=None
)
RUN_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "run_id", default=None
)
SESSION_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "session_id", default=None
)
CONVERSATION_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "conversation_id", default=None
)
TENANT_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tenant_id", default=None
)
USER_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "user_id", default=None
)
PROJECT_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "project_id", default=None
)


def set_correlation_id(value: str | None) -> None:
    CORRELATION_ID.set(value)


def set_trace_ids(trace_id: str | None, span_id: str | None) -> None:
    TRACE_ID.set(trace_id)
    SPAN_ID.set(span_id)


@contextlib.contextmanager
def run_context(
    *,
    run_id: str | None = None,
    session_id: str | None = None,
    conversation_id: str | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    project_id: str | None = None,
) -> Iterator[None]:
    """Set run identity contextvars for the duration of a block."""

    tokens = [
        RUN_ID.set(run_id),
        SESSION_ID.set(session_id),
        CONVERSATION_ID.set(conversation_id),
        TENANT_ID.set(tenant_id),
        USER_ID.set(user_id),
        PROJECT_ID.set(project_id),
    ]
    try:
        yield
    finally:
        for token in tokens:
            token.var.reset(token)


def snapshot() -> dict[str, str | None]:
    return {
        "correlation_id": CORRELATION_ID.get(),
        "trace_id": TRACE_ID.get(),
        "span_id": SPAN_ID.get(),
        "run_id": RUN_ID.get(),
        "session_id": SESSION_ID.get(),
        "conversation_id": CONVERSATION_ID.get(),
        "tenant_id": TENANT_ID.get(),
        "user_id": USER_ID.get(),
        "project_id": PROJECT_ID.get(),
    }


__all__ = [
    "CONVERSATION_ID",
    "CORRELATION_ID",
    "PROJECT_ID",
    "RUN_ID",
    "SESSION_ID",
    "SPAN_ID",
    "TENANT_ID",
    "TRACE_ID",
    "USER_ID",
    "run_context",
    "set_correlation_id",
    "set_trace_ids",
    "snapshot",
]

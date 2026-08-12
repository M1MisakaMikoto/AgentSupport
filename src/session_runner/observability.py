"""Runner-side structured logging and context (independent of agentsupport)."""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import socket
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

CORRELATION_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
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

_configured = False


def snapshot() -> dict[str, str | None]:
    return {
        "correlation_id": CORRELATION_ID.get(),
        "run_id": RUN_ID.get(),
        "session_id": SESSION_ID.get(),
        "conversation_id": CONVERSATION_ID.get(),
    }


def set_correlation_id(value: str | None) -> None:
    CORRELATION_ID.set(value)


@contextlib.contextmanager
def run_context(
    *,
    run_id: str | None = None,
    session_id: str | None = None,
    conversation_id: str | None = None,
) -> Iterator[None]:
    tokens = [
        RUN_ID.set(run_id),
        SESSION_ID.set(session_id),
        CONVERSATION_ID.set(conversation_id),
    ]
    try:
        yield
    finally:
        for token in tokens:
            token.var.reset(token)


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in snapshot().items():
            setattr(record, key, value or "")
        record.service = os.getenv("AGENTSUPPORT_SERVICE_NAME", "session-runner")
        record.instance_id = f"{socket.gethostname()}:{os.getpid()}"
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": getattr(record, "service", "session-runner"),
            "instance_id": getattr(record, "instance_id", ""),
        }
        for key in ("correlation_id", "run_id", "session_id", "conversation_id"):
            value = getattr(record, key, None)
            if value:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    *,
    log_format: str | None = None,
    level: str | None = None,
    service_name: str | None = None,
) -> None:
    global _configured
    if _configured:
        return
    _configured = True
    if service_name:
        os.environ.setdefault("AGENTSUPPORT_SERVICE_NAME", service_name)
    fmt = log_format or os.getenv("AGENTSUPPORT_LOG_FORMAT", "console")
    lvl = level or os.getenv("AGENTSUPPORT_LOG_LEVEL", "INFO")
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [%(service)s %(instance_id)s"
                " run=%(run_id)s] %(name)s: %(message)s"
            )
        )
    handler.addFilter(_ContextFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(lvl.upper())
    logging.getLogger("uvicorn").handlers = []
    logging.getLogger("uvicorn.access").propagate = False


__all__ = ["configure_logging", "run_context", "set_correlation_id", "snapshot"]

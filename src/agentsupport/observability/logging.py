"""Structured logging with request/run context enrichment."""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
from datetime import UTC, datetime
from typing import Any

from .context import snapshot

_configured = False


class ContextFilter(logging.Filter):
    """Attach request/run context fields to every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in snapshot().items():
            setattr(record, key, value or "")
        record.service = os.getenv("AGENTSUPPORT_SERVICE_NAME", "agentsupport")
        record.instance_id = (
            f"{socket.gethostname()}:{os.getpid()}"
        )
        return True


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": getattr(record, "service", "agentsupport"),
            "instance_id": getattr(record, "instance_id", ""),
        }
        for key in (
            "correlation_id",
            "trace_id",
            "span_id",
            "run_id",
            "session_id",
            "conversation_id",
            "tenant_id",
            "user_id",
            "project_id",
        ):
            value = getattr(record, key, None)
            if value:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if hasattr(record, "extra_fields") and isinstance(record.extra_fields, dict):
            payload.update(record.extra_fields)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    *,
    log_format: str = "console",
    level: str = "INFO",
    service_name: str | None = None,
) -> None:
    """Idempotently configure root logging with context enrichment."""

    global _configured
    if _configured:
        return
    _configured = True
    if service_name:
        os.environ.setdefault("AGENTSUPPORT_SERVICE_NAME", service_name)
    handler = logging.StreamHandler(sys.stdout)
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [%(service)s %(instance_id)s"
                " corr=%(correlation_id)s run=%(run_id)s] %(name)s: %(message)s"
            )
        )
    handler.addFilter(ContextFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    logging.getLogger("uvicorn").handlers = []
    logging.getLogger("uvicorn.access").propagate = False


__all__ = ["ContextFilter", "JsonFormatter", "configure_logging"]

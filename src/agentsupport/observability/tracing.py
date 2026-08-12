"""OpenTelemetry tracing bootstrap (optional ``observability`` extra)."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from typing import Any

from .context import set_trace_ids

logger = logging.getLogger(__name__)
_trace_enabled = False


def tracing_enabled() -> bool:
    return _trace_enabled


def init_tracing(*, service_name: str, endpoint: str | None) -> bool:
    global _trace_enabled
    if not endpoint:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "opentelemetry extras are not installed; tracing disabled "
            "(pip install '.[observability]')"
        )
        return False
    resource = Resource.create({SERVICE_NAME: service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    SQLAlchemyInstrumentor().instrument()
    _trace_enabled = True
    return True


def instrument_app(app: Any) -> None:
    if not _trace_enabled:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        return
    FastAPIInstrumentor.instrument_app(app)


def sync_trace_ids() -> None:
    """Copy the current OpenTelemetry span ids into logging contextvars."""

    if not _trace_enabled:
        return
    try:
        from opentelemetry import trace
    except ImportError:
        return
    span = trace.get_current_span()
    context = span.get_span_context()
    if context is not None and context.is_valid:
        set_trace_ids(
            format(context.trace_id, "032x"),
            format(context.span_id, "016x"),
        )
    else:
        set_trace_ids(None, None)


@contextlib.contextmanager
def start_span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
    if not _trace_enabled:
        yield None
        return
    try:
        from opentelemetry import trace
    except ImportError:
        yield None
        return
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span(name, attributes=attributes or {}) as span:
        sync_trace_ids()
        yield span
    set_trace_ids(None, None)


__all__ = ["init_tracing", "instrument_app", "start_span", "sync_trace_ids", "tracing_enabled"]

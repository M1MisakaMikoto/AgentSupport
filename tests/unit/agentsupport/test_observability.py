"""Observability: context propagation, structured logging and Prometheus metrics."""

import json
import logging

import pytest
from httpx import ASGITransport, AsyncClient

from agentsupport.api import create_app
from agentsupport.config import Settings
from agentsupport.observability import context as obs_context
from agentsupport.observability import logging as obs_logging
from agentsupport.observability import metrics as obs_metrics
from agentsupport.observability import tracing as obs_tracing
from _support import make_temporal_service as _make_service


def test_run_context_sets_and_restores_values():
    assert obs_context.snapshot()["run_id"] is None
    with obs_context.run_context(run_id="r1", session_id="s1"):
        assert obs_context.snapshot()["run_id"] == "r1"
        with obs_context.run_context(run_id="r2"):
            assert obs_context.snapshot()["run_id"] == "r2"
        assert obs_context.snapshot()["run_id"] == "r1"
    assert obs_context.snapshot()["run_id"] is None


def test_json_formatter_includes_context_fields():
    formatter = obs_logging.JsonFormatter()
    obs_context.set_correlation_id("c-1")
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    filter_ = obs_logging.ContextFilter()
    assert filter_.filter(record) is True
    payload = json.loads(formatter.format(record))
    assert payload["message"] == "hello"
    assert payload["correlation_id"] == "c-1"
    assert payload["service"] == "agentsupport"


def test_metrics_registry_publishes_and_renders():
    obs_metrics.publish_coordination(
        {"queue_ready": 3, "active_runtimes": 2, "outbox_pending": 1}
    )
    obs_metrics.record_http("GET", "/live", 200, 0.01)
    obs_metrics.record_run("completed", 1.5)
    obs_metrics.record_skill_upload()
    obs_metrics.record_runner_heartbeat_expired()
    obs_metrics.set_runners_registered(2)

    body, content_type = obs_metrics.render_metrics()
    text = body.decode()
    assert content_type.startswith("text/plain")
    for name in (
        "agentsupport_queue_ready",
        "agentsupport_active_runtimes",
        "agentsupport_outbox_pending",
        "agentsupport_http_requests_total",
        "agentsupport_run_total",
        "agentsupport_skill_uploads_total",
        "agentsupport_runner_heartbeat_expired_total",
        "agentsupport_runners_registered",
    ):
        assert name in text, name
    assert 'method="GET"' in text


@pytest.mark.asyncio
async def test_metrics_endpoint_exposes_registry(tmp_path):
    service = _make_service(tmp_path).service
    app = create_app(service)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        live = await client.get("/live")
        assert live.status_code == 200
        response = await client.get("/metrics")
        assert response.status_code == 200
        text = response.text
        assert 'agentsupport_http_requests_total{method="GET",route="/live",status="200"}' in text
        assert "agentsupport_queue_ready" in text
        assert "agentsupport_run_duration_seconds_bucket" in text


def test_tracing_is_noop_without_endpoint():
    assert obs_tracing.init_tracing(service_name="test", endpoint=None) is False
    with obs_tracing.start_span("probe") as span:
        assert span is None

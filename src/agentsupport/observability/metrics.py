"""Prometheus metrics for the AgentSupport control plane."""

from __future__ import annotations

from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

REGISTRY = CollectorRegistry(auto_describe=False)

# ------------------------------------------------------------------ legacy
queue_ready = Gauge(
    "agentsupport_queue_ready", "Ready jobs waiting for a worker", registry=REGISTRY
)
queue_oldest_ready_seconds = Gauge(
    "agentsupport_queue_oldest_ready_seconds",
    "Age of the oldest ready job",
    registry=REGISTRY,
)
jobs_claimed = Gauge(
    "agentsupport_jobs_claimed", "Jobs currently claimed by workers", registry=REGISTRY
)
jobs_running = Gauge(
    "agentsupport_jobs_running", "Jobs currently running", registry=REGISTRY
)
jobs_waiting = Gauge(
    "agentsupport_jobs_waiting", "Jobs waiting for input", registry=REGISTRY
)
jobs_paused = Gauge(
    "agentsupport_jobs_paused", "Jobs paused for checkpoint recovery", registry=REGISTRY
)
claims_expired = Gauge(
    "agentsupport_claims_expired", "Expired job claims observed", registry=REGISTRY
)
active_runtimes = Gauge(
    "agentsupport_active_runtimes", "Active session containers", registry=REGISTRY
)
runner_starting = Gauge(
    "agentsupport_runner_starting", "Runners currently starting", registry=REGISTRY
)
runner_reconciliation_needed = Gauge(
    "agentsupport_runner_reconciliation_needed",
    "Runner endpoints awaiting reconciliation",
    registry=REGISTRY,
)
workspace_lease_contention = Gauge(
    "agentsupport_workspace_lease_contention",
    "Workspace lease acquisition failures",
    registry=REGISTRY,
)
outbox_pending = Gauge(
    "agentsupport_outbox_pending", "Unpublished outbox events", registry=REGISTRY
)
outbox_publication_lag_seconds = Gauge(
    "agentsupport_outbox_publication_lag_seconds",
    "Age of the oldest pending outbox event",
    registry=REGISTRY,
)

# ------------------------------------------------------------------ http
http_requests = Counter(
    "agentsupport_http_requests_total",
    "HTTP requests handled by the control plane",
    ["method", "route", "status"],
    registry=REGISTRY,
)
http_request_duration = Histogram(
    "agentsupport_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    registry=REGISTRY,
)

# ------------------------------------------------------------------ runs
run_total = Counter(
    "agentsupport_run_total",
    "Completed runs by terminal outcome",
    ["outcome"],
    registry=REGISTRY,
)
run_duration = Histogram(
    "agentsupport_run_duration_seconds",
    "Run wall-clock duration by terminal outcome",
    ["outcome"],
    registry=REGISTRY,
)
queue_wait = Histogram(
    "agentsupport_queue_wait_seconds",
    "Time a job waits before being claimed",
    registry=REGISTRY,
)

# ------------------------------------------------------------------ self-service
skill_uploads = Counter(
    "agentsupport_skill_uploads_total",
    "Skills uploaded by upstream",
    registry=REGISTRY,
)
runners_registered = Gauge(
    "agentsupport_runners_registered",
    "Runners currently registered and ready",
    registry=REGISTRY,
)
runner_heartbeat_expired = Counter(
    "agentsupport_runner_heartbeat_expired_total",
    "Runners expired due to missed heartbeats",
    registry=REGISTRY,
)

_GAUGES: dict[str, Gauge] = {
    "queue_ready": queue_ready,
    "queue_oldest_ready_seconds": queue_oldest_ready_seconds,
    "jobs_claimed": jobs_claimed,
    "jobs_running": jobs_running,
    "jobs_waiting": jobs_waiting,
    "jobs_paused": jobs_paused,
    "claims_expired": claims_expired,
    "active_runtimes": active_runtimes,
    "runner_starting": runner_starting,
    "runner_reconciliation_needed": runner_reconciliation_needed,
    "workspace_lease_contention": workspace_lease_contention,
    "outbox_pending": outbox_pending,
    "outbox_publication_lag_seconds": outbox_publication_lag_seconds,
}


def publish_coordination(values: dict[str, Any]) -> None:
    for name, value in values.items():
        gauge = _GAUGES.get(name)
        if gauge is not None:
            gauge.set(float(value or 0))


def record_http(method: str, route: str, status: int, duration: float) -> None:
    http_requests.labels(method=method, route=route, status=str(status)).inc()
    http_request_duration.labels(method=method, route=route).observe(duration)


def record_run(outcome: str, duration: float) -> None:
    run_total.labels(outcome=outcome).inc()
    run_duration.labels(outcome=outcome).observe(duration)


def record_queue_wait(seconds: float) -> None:
    queue_wait.observe(seconds)


def record_skill_upload() -> None:
    skill_uploads.inc()


def set_runners_registered(count: int) -> None:
    runners_registered.set(float(count))


def record_runner_heartbeat_expired() -> None:
    runner_heartbeat_expired.inc()


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


__all__ = [
    "publish_coordination",
    "record_http",
    "record_queue_wait",
    "record_run",
    "record_runner_heartbeat_expired",
    "record_skill_upload",
    "render_metrics",
    "set_runners_registered",
]

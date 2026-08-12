# 可观测性

AgentSupport 提供三层可观测性：结构化日志、Prometheus 指标与 OpenTelemetry 追踪。

## 1. 结构化日志

所有服务（API / Worker / Reconciler / Retention / Event Publisher / Session Runner）输出带
请求与运行上下文的日志。默认控制台格式，生产建议 JSON：

```text
AGENTSUPPORT_LOG_FORMAT=json   # console | json
AGENTSUPPORT_LOG_LEVEL=INFO
AGENTSUPPORT_SERVICE_NAME=agentsupport
```

日志字段：`ts` / `level` / `logger` / `message` / `service` / `instance_id`，以及
`correlation_id`、`trace_id`、`span_id`、`run_id`、`session_id`、`conversation_id`、
`tenant_id`、`user_id`、`project_id`（有值时出现）。异步任务与 Worker claim 自动继承上下文。

## 2. 指标

`GET /metrics`（控制面）与 Session Runner 的 `/metrics` 输出 Prometheus 文本格式。

控制面指标（`agentsupport_` 前缀）：

- 协调：`queue_ready`、`queue_oldest_ready_seconds`、`jobs_claimed`、`jobs_running`、
  `jobs_waiting`、`jobs_paused`、`claims_expired`、`active_runtimes`、
  `runner_starting`、`runner_reconciliation_needed`、`workspace_lease_contention`、
  `outbox_pending`、`outbox_publication_lag_seconds`
- HTTP：`http_requests_total{method,route,status}`、`http_request_duration_seconds{method,route}`
- Run：`run_total{outcome}`、`run_duration_seconds{outcome}`、`queue_wait_seconds`
- 自服务：`skill_uploads_total`、`runners_registered`、`runner_heartbeat_expired_total`

Runner 指标（`session_runner_` 前缀）：`http_requests_total`、`mcp_connections_total{result}`。

## 3. 告警与看板

```powershell
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d
```

- Prometheus：http://localhost:9090 （抓取 `api:8000/metrics` 与 `runner:8080/metrics`，
  告警规则见 `deploy/observability/alerts.yml`）
- Grafana：http://localhost:3000 （admin / admin，内置 AgentSupport 看板：
  HTTP 吞吐与 p95 时延、任务队列、Run 结果分布、Runner 与 MCP 连接）

## 4. 追踪（可选）

安装可选依赖并配置 OTLP 端点后开启：

```powershell
pip install -e ".[observability]"
$env:AGENTSUPPORT_OTEL_EXPORTER_OTLP_ENDPOINT="http://otel-collector:4318/v1/traces"
```

- 入站接受标准 `traceparent`，未传时生成；`X-Correlation-ID` 继续作为业务关联 ID。
- 已插桩：FastAPI 入站、httpx 出站（Worker → Runner）、SQLAlchemy。
- 日志中的 `trace_id` / `span_id` 与追踪链路一致。
- 未配置端点时全部为 no-op，不影响行为。

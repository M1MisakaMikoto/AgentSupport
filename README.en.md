> [简体中文](README.md) | English

# AgentSupport

AgentSupport is a self-hosted service for running coding agents through an HTTP API. It manages
workspaces, sessions and conversations in a control plane, while isolated Session Runners execute
tasks, tools and model interactions.

Key capabilities include:

- Durable Workspace, Session and Conversation resources.
- Ordered event history and Server-Sent Events (SSE) replay.
- Idempotent commands, optimistic concurrency, approvals, cancellation and checkpoint recovery.
- Inline development mode and distributed PostgreSQL-backed execution.
- Controlled Trae, MCP, Skill and workspace tool integration.
- Docker Compose and Kubernetes deployment manifests.

## Documentation

- [AgentSupport API reference (Chinese)](docs/api/agentsupport-api.md)
- [API full-test plan (Chinese)](docs/testing/api-full-test-plan.md)
- [Observability](docs/observability.md)
- [Directory architecture](docs/architecture/directory-structure.en.md)
- [Architecture decision records](docs/adr/)
- [简体中文 README](README.md)

## Project structure

Production packages use the `src/` layout:

| Package | Responsibility |
| --- | --- |
| `agentsupport` | Control-plane API, orchestration, persistence and distributed workers |
| `session_runner` | Private execution-plane HTTP service and Trae/MCP/tool adapters |
| `agent_runner_contracts` | Wire models shared by the control and execution planes |
| `devtools` | Local deployment and task-debugging console |

## Requirements

- Python 3.12
- Docker Engine with the Compose plugin for the distributed stack
- PostgreSQL 16 and Redis 7 when running distributed services outside Compose
- A Kubernetes cluster and `kubectl` for Kubernetes deployment

## Local development

Create a virtual environment and install development dependencies:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Run the test suite. Using a repository-local temporary directory avoids Windows temp-directory
permission issues:

```powershell
$env:TEST_TMP=(New-Item -ItemType Directory -Force .pytest-tmp).FullName
$env:TEMP=$env:TEST_TMP
$env:TMP=$env:TEMP
.venv\Scripts\python.exe -m pytest -q
```

Run the API in its default in-memory, inline execution mode:

```powershell
.venv\Scripts\python.exe -m uvicorn agentsupport.main:app --reload
```

Useful local endpoints:

| URL | Purpose |
| --- | --- |
| `http://127.0.0.1:8000/docs` | OpenAPI interface |
| `http://127.0.0.1:8000/debug/` | Task debugging interface |
| `http://127.0.0.1:8000/live` | Liveness check |
| `http://127.0.0.1:8000/ready` | Readiness and active configuration |
| `http://127.0.0.1:8000/metrics` | Prometheus metrics |

## API authentication and precondition auto-completion

The AgentSupport public API **intentionally ships without token / API Key authentication**.
This is a contract enforced by `tests/contract/agentsupport_api/test_no_auth.py`:
`AGENTSUPPORT_API_AUTH_MODE` only accepts `none`, and any other value fails startup. Add
rate limiting at the gateway before exposing the API publicly (the repository provides no
formal authentication or tenant isolation).

The platform manages execution resources (Workspace / Session / Conversation) only. Tenant / user /
project entities are managed by the upstream system; the platform passes them through as optional
`tenant_id` / `user_id` / `project_id` labels (no existence checks, carried into events and
audit). Execution configuration travels with the Session request; preset entities no longer exist.

**v0.2 auto-create semantics**: only an explicitly passed, missing `workspace_id` / `session_id` is
created with that ID; fields the caller did not pass are never generated (no default Workspace /
default user). Auto-created resources are surfaced via the `auto_created` field in `201` responses
plus `workspace.created` / `session.created` events. Streaming / run-state endpoints still require
the resource to exist; read-only queries never auto-create and keep returning `404`.

Auto-completion is enabled by default and should be disabled in production:

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENTSUPPORT_AUTO_CREATE_MISSING` | `true` | Master switch; `false` restores strict `404` behavior |
| `AGENTSUPPORT_AUTO_CREATE_SCOPES` | `workspace,session` | Only execution resources take part in auto-create |

Runner self-registration (internal contract) uses a shared token:

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENTSUPPORT_RUNNER_TOKEN` | empty | Control-plane Runner registration token; empty disables registration |
| `AGENTSUPPORT_RUNNER_HEARTBEAT_TIMEOUT_SECONDS` | `30` | Heartbeat timeout; stale Runners are cleaned up |
| `SESSION_RUNNER_TOKEN` | empty | Same shared token on the Runner side |
| `AGENTSUPPORT_CONTROL_PLANE_URL` | empty | Runner registration target URL |
| `SESSION_RUNNER_ENDPOINT` | empty | Runner-advertised reachable URL |

Upstream can self-service agent capabilities: the [Skill API](docs/api/agentsupport-api.md#7-skill-api)
uploads/discover skills and the [MCP Server API](docs/api/agentsupport-api.md#8-mcp-server-api)
registers MCP connection definitions; both are referenced per Session or per Conversation.

The private Runner can be started separately in deterministic mode when working on its HTTP
contract:

```powershell
$env:SESSION_RUNNER_MODE="deterministic"
.venv\Scripts\python.exe -m uvicorn session_runner.main:app --port 8080
```

## Development console

On Windows, launch the local deployment console with:

```powershell
.\start-console.ps1
```

Use `-NoBrowser` to start it without opening a browser. The equivalent direct command is:

```powershell
.venv\Scripts\python.exe -m devtools.console
```

Open `http://127.0.0.1:8010`. The console manages the Compose stack, service scaling, status,
logs, task debugging and the repository's predefined validation suites. It runs outside the
Compose stack, so it remains available while containers are replaced.

The console is organized as an AI-software-style platform with exactly two sections:

| Section | Pages | Capability |
| --- | --- | --- |
| Demo | Overview / Agent Workspace | Execution-resource overview (Workspace / Session / Conversation) and the label model; the Agent Workspace and the task-debug page create/select Workspaces and Sessions (with `tenant_id` / `user_id` / `project_id` labels), submit tasks with selectable Skill and MCP Server references activated per conversation, provide an SSE event timeline with input/approval/cancel gates, and offer a model-connectivity self-check that probes the model API's TLS certificate from the Runner. Business entities are managed upstream; the console no longer ships organization/user/project/preset management pages |
| Deployment | Service Status / Deploy Actions / Acceptance / API Reference | Compose service topology and health endpoints (`/live` `/ready` `/metrics` `/cores`), deploy/start/stop and scaling, deployment regression plus API contract acceptance (structured assertions across all public operations, error paths, idempotency, optimistic concurrency, SSE resume and an OpenAPI operation-coverage report), and an interactive API reference generated from the runtime `/openapi.json` |

API contract acceptance is a repeatable in-console check; release criteria still follow the
[API full-test plan](docs/testing/api-full-test-plan.md).

The console supports WSL2 Docker Engine, a local `docker` CLI and named Docker contexts. Configure
the default transport when automatic detection is not suitable:

```powershell
$env:AGENTSUPPORT_DEV_DOCKER_TRANSPORT="wsl2" # wsl2, local, or context
$env:AGENTSUPPORT_DEV_WSL_DISTRIBUTION="Ubuntu-24.04"
$env:AGENTSUPPORT_DEV_DOCKER_CONTEXT=""
.\start-console.ps1
```

For repositories on a Windows drive, prefer the console for WSL2 builds; it stages Docker build
inputs in the WSL filesystem to avoid DrvFS metadata limitations.

## Example Skills

The `skills/` directory at the repository root ships two uploadable example skills (`review` and
`docs-writing`). Local development uses the default `AGENTSUPPORT_SKILLS_ROOT=skills`, so the
console discovers them immediately; Docker Compose uses a dedicated `skills-data` named volume,
so upload them once via `POST /skills` (multipart, a `SKILL.md` file or zip) before they appear in
the selection lists of the console and the task-debug page. Enabled skills are delivered with each
run request: their `SKILL.md` content is injected into the Trae agent's system prompt (truncated
beyond roughly 20K characters per skill) and `skills-data` is mounted read-only into the Runner
for auxiliary files.

## Docker Compose

The Compose stack contains the API gateway, PostgreSQL, Redis, database migration job, API,
Worker, Reconciler, Event Publisher and Session Runner.

For deterministic execution without model access:

```powershell
$env:SESSION_RUNNER_MODE="deterministic"
docker compose up -d --build
```

For Trae execution, configure the model provider before starting the stack:

```powershell
$env:SESSION_RUNNER_MODE="trae"
$env:TRAE_PROVIDER="anthropic"
$env:TRAE_MODEL="claude-sonnet-4-20250514"
$env:TRAE_API_KEY="<api-key>"
docker compose up -d --build
```

The API is exposed at `http://localhost:8000` and PostgreSQL at `localhost:5432`. Inspect or stop
the stack with:

```powershell
docker compose ps
docker compose logs --tail 200 api worker runner
docker compose down
```

`docker compose down` preserves the named data volumes unless `--volumes` is explicitly supplied.

### Alternative model providers

DeepSeek's Anthropic-compatible endpoint uses the dedicated provider implementation:

```powershell
$env:TRAE_PROVIDER="deepseek_anthropic"
$env:TRAE_MODEL_BASE_URL="https://api.deepseek.com/anthropic"
$env:TRAE_MODEL="<model-name>"
$env:TRAE_API_KEY="<api-key>"
```

For an OpenAI-compatible endpoint:

```powershell
$env:TRAE_PROVIDER="openai"
$env:TRAE_MODEL_BASE_URL="https://example.com/v1"
$env:TRAE_MODEL="<model-name>"
$env:TRAE_API_KEY="<api-key>"
```

Model credentials are passed only to the Runner service. Do not commit credentials to `.env` or
other repository files.

## Configuration

`.env.example` lists the supported local and distributed settings. Important variables include:

| Variable | Purpose |
| --- | --- |
| `AGENTSUPPORT_PERSISTENCE_MODE` | `memory` or `postgres` persistence |
| `AGENTSUPPORT_EXECUTION_MODE` | `inline` or `distributed` execution |
| `AGENTSUPPORT_DATABASE_URL` | SQLAlchemy PostgreSQL URL |
| `AGENTSUPPORT_REDIS_URL` | Optional Redis event notification URL |
| `AGENTSUPPORT_RUNTIME_DRIVER` | `memory`, `docker_cli`, or `kubernetes` runtime |
| `AGENTSUPPORT_WORKSPACE_ROOT` | Workspace data directory |
| `AGENTSUPPORT_MAX_ACTIVE_SESSIONS` | Concurrent active Session limit |
| `AGENTSUPPORT_MAX_QUEUED_CONVERSATIONS` | Conversation queue limit |
| `AGENTSUPPORT_AUTO_CREATE_MISSING` | Missing-precondition auto-completion switch (default on) |
| `AGENTSUPPORT_API_AUTH_MODE` | API auth mode; only `none` is supported |
| `AGENTSUPPORT_RUNNER_TOKEN` | Runner self-registration shared token (empty disables registration) |
| `SESSION_RUNNER_MODE` | `deterministic` or `trae` Runner mode |
| `TRAE_PROVIDER` | Model provider implementation |
| `TRAE_MODEL`, `TRAE_MODEL_BASE_URL`, `TRAE_API_KEY` | Runner model configuration |

## Database migrations

Compose applies Alembic migrations before starting the API and workers. For an external
PostgreSQL instance, set `AGENTSUPPORT_DATABASE_URL` and run:

```powershell
$env:AGENTSUPPORT_AUTO_CREATE_SCHEMA="false"
.venv\Scripts\python.exe -m alembic upgrade head
```

Production and distributed deployments should keep automatic schema creation disabled.

## Distributed operation

API and Worker processes are stateless and can be scaled independently. PostgreSQL is the source
of truth for jobs, events, commands, checkpoints and leases; Redis only accelerates subscriber
wake-ups.

```powershell
$env:SESSION_RUNNER_MODE="deterministic"
docker compose up -d --build --scale api=2 --scale worker=3
Invoke-WebRequest -UseBasicParsing http://localhost:8000/ready
Invoke-WebRequest -UseBasicParsing http://localhost:8000/metrics
```

Workers use expiring claims and fence epochs. A paused Run releases execution capacity, and a
replacement Runner resumes from a validated checkpoint.

## Kubernetes

Kubernetes deployment requires an external PostgreSQL database and published AgentSupport API and
Runner images. Create the namespace and secrets, run the migration Job, then apply the services:

```powershell
kubectl apply -f deploy/kubernetes/namespace.yaml
kubectl -n agentsupport create secret generic agentsupport-secrets --from-literal=AGENTSUPPORT_DATABASE_URL='<postgresql+psycopg URL>'
kubectl -n agentsupport create secret generic agentsupport-runner-secrets --from-literal=TRAE_API_KEY='<api-key>'
kubectl apply -f deploy/kubernetes/migrate-job.yaml
kubectl -n agentsupport wait --for=condition=complete job/agentsupport-db-migrate --timeout=5m
kubectl apply -f deploy/kubernetes/agentsupport.yaml
```

Install KEDA before applying `deploy/kubernetes/keda-worker.yaml`. Without KEDA, configure a fixed
Worker replica count. Workspace PVCs require a StorageClass compatible with `ReadWriteOnce`.

## Production considerations

The current repository does not provide formal authentication, tenant isolation, managed
MCP/Skill marketplace integration, object-storage backups or multi-region coordination. Deploy it
behind an authenticated gateway and define database, workspace-volume and secret backup policies
before production use. Because the API has no authentication and precondition auto-completion is
enabled by default (a single request can create a whole resource chain), configure rate limiting at
the gateway before public exposure, and disable auto-completion
(`AGENTSUPPORT_AUTO_CREATE_MISSING=false`) when appropriate.

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
- [Directory architecture](docs/architecture/directory-structure.md)
- [Architecture decision records](docs/adr/)

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
before production use.

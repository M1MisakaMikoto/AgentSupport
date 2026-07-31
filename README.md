# Agent Platform

This repository contains the first implementation slice from `.dev/lab/confirmation/accepted.md`.
It is an API-first Python 3.12 service with a private Session Runner contract.

Production Python packages use the `src/` layout. `agent_platform` is the control plane,
`session_runner` is the execution plane, and `agent_runner_contracts` contains their only shared
wire models.

## Documentation

- [Platform API reference (Chinese)](docs/api/platform-api.md)
- [Directory architecture](docs/architecture/directory-structure.md)

## Local setup

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
$env:TEST_TMP=(New-Item -ItemType Directory -Force .pytest-tmp).FullName
$env:TEMP=$env:TEST_TMP
$env:TMP=$env:TEMP
.venv\Scripts\python.exe -m pytest -q
```

Run the platform API:

```powershell
.venv\Scripts\python.exe -m uvicorn agent_platform.main:app --reload
```

On Windows, double-click `start-console.cmd`, or run the launcher from PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start-console.ps1
```

The launcher reuses an existing console when possible, otherwise starts it in the background,
waits for readiness and opens the browser. To start it without opening a browser, pass
`-NoBrowser`.

The equivalent direct command is:

```powershell
.venv\Scripts\python.exe -m devtools.console
```

Open `http://127.0.0.1:8010`. The same-origin console provides deployment control at `/` and
runtime task debugging at `/tasks/`; browser API and SSE traffic is routed through `/platform/`
to the fixed local platform endpoint. It can build, start, scale and stop the Compose
stack, display service status and logs, and run the controlled short/PostgreSQL/live acceptance
suite. It accepts only predefined operations and never removes data volumes. Keep this local
development process outside the Compose stack so it remains available while API containers are
being created or replaced. The direct platform debug URL at `http://127.0.0.1:8000/debug/`
remains available for compatibility.

The deployment console supports three Docker transports: direct WSL2 execution, a local
`docker` CLI, and a named Docker context. On Windows, automatic mode uses the default WSL2
distribution and does not require Docker Desktop or a Windows `docker.exe`. Select a distribution
in the console when the WSL default is not the one that hosts Docker. The equivalent console
defaults can be set before launching it:

```powershell
$env:AGENT_DEV_DOCKER_TRANSPORT = "wsl2"
$env:AGENT_DEV_WSL_DISTRIBUTION = "Ubuntu-24.04"
.\start-console.ps1
```

For a remote or Docker Desktop context, set `AGENT_DEV_DOCKER_TRANSPORT=context` and
`AGENT_DEV_DOCKER_CONTEXT=<context-name>`. Use `local` when `docker` is directly available on
the host PATH.

Run the private runner:

```powershell
.venv\Scripts\python.exe -m uvicorn session_runner.main:app --port 8080
```

## WSL2 Docker development

Install Docker Engine and the Compose plugin inside a WSL2 distribution, then run Compose from
PowerShell without installing Docker Desktop or a Windows Docker CLI:

```powershell
$distro = "Ubuntu-24.04"
wsl.exe --distribution $distro --cd $PWD.Path --exec docker version
wsl.exe --distribution $distro --cd $PWD.Path --exec docker compose config --quiet
wsl.exe --distribution $distro --cd $PWD.Path --exec docker compose ps
Invoke-WebRequest -UseBasicParsing http://localhost:8000/live
```

Use the deployment console for builds from a project stored on a Windows drive. It stages only
the Dockerfile inputs under WSL `/tmp` before invoking BuildKit, avoiding DrvFS `xattr` failures
caused by Windows-owned cache directories. A manual `docker compose build` from `/mnt/c` or
`/mnt/d` can still hit that limitation; manual builds should use a clone stored in the WSL-native
filesystem.

If Docker is not running in the distribution, start its service there and rerun the console
connection check:

```powershell
wsl.exe --distribution $distro --user root --exec service docker start
wsl.exe --distribution $distro --exec docker version
```

Compose starts the Session Runner in real Trae mode. Set `TRAE_API_KEY` and, when
needed, `TRAE_MODEL_BASE_URL` in the environment before starting the stack; secrets
are passed to the Runner process only and are never written to the YAML config or image.
The vendored MIT source snapshot is under `vendor/trae-agent-src` and is invoked only
through Trae's `_tool_caller` boundary.

For DeepSeek's Anthropic-compatible endpoint, select its dedicated provider so local tools use
the custom-tool subset documented by DeepSeek instead of Anthropic's native versioned tools:

```powershell
$env:TRAE_PROVIDER = "deepseek_anthropic"
$env:TRAE_API_KEY = "<temporary-api-key>"
$env:TRAE_MODEL_BASE_URL = "https://api.deepseek.com/anthropic"
$env:TRAE_MODEL = "deepseek-v4-pro"
wsl.exe --distribution $distro --cd $PWD.Path --exec docker compose up -d --force-recreate --no-build
```

For an OpenAI-compatible provider, override the provider and model in the same shell:

```powershell
$env:TRAE_PROVIDER = "openai"
$env:TRAE_API_KEY = "<temporary-api-key>"
$env:TRAE_MODEL_BASE_URL = "https://example.com/compatible-mode/v1"
$env:TRAE_MODEL = "<model-name>"
wsl.exe --distribution $distro --cd $PWD.Path --exec docker compose up -d --force-recreate --no-build
```

For rapid source-only iterations after the first successful image build, reuse the
existing dependency image and avoid pip index downloads:

```powershell
wsl.exe --distribution $distro --cd $PWD.Path --exec docker build --build-arg BASE_IMAGE=agentsupport-api:latest --build-arg INSTALL_DEPS=false -t agentsupport-api:latest .
wsl.exe --distribution $distro --cd $PWD.Path --exec docker compose up -d --force-recreate --no-build
```

The Compose API is exposed at `http://localhost:8000` and PostgreSQL at `localhost:5432`.
WSL localhost forwarding must be enabled for those endpoints to be reachable from Windows.

The temporary acceptance console is served by the API at
`http://localhost:8000/debug/`. It creates the Workspace, Session and Conversation
through public platform routes and exposes event replay, SSE status, approvals, input
and cancellation. It never accepts or stores model credentials.

`docker compose up --build` starts PostgreSQL, Redis, a one-shot Alembic migration,
the stateless API, Nginx gateway, Worker, Reconciler, Event Publisher and Runner.
PostgreSQL is authoritative; Redis only accelerates subscriber wake-ups, so accepted
work and event replay continue through database polling during a Redis outage.
Resources and events survive API, Worker and Runner replacement. Local Python execution
still defaults to the in-memory repository for deterministic tests.

After PostgreSQL is available, initialize the explicit schema with:

```powershell
.venv\Scripts\python.exe -m agent_platform.db
```

Production and distributed deployments must use Alembic instead of application startup
schema creation:

```powershell
$env:AGENT_PLATFORM_AUTO_CREATE_SCHEMA = "false"
.venv\Scripts\python.exe -m alembic upgrade head
```

## Distributed operation

The API is stateless in `AGENT_PLATFORM_EXECUTION_MODE=distributed`. Workers claim durable
jobs with expiring leases, claim tokens and fence epochs. A Session Runner remains a routed,
stateful endpoint: it is created per active Session, never load-balanced across unrelated Runs,
and is removed when the Run becomes terminal. A paused Run releases execution capacity and a
replacement Runner resumes from its validated checkpoint.

Scale API and Worker processes independently in Compose:

```powershell
$env:SESSION_RUNNER_MODE = "deterministic"
docker compose up -d --build --scale api=2 --scale worker=3
docker compose ps
Invoke-WebRequest -UseBasicParsing http://localhost:8000/ready
Invoke-WebRequest -UseBasicParsing http://localhost:8000/metrics
```

For Kubernetes, PostgreSQL is an external prerequisite. Build and publish both
`agentsupport-api:latest` and the configured Runner image, then create the namespace and
secrets before running the migration Job. `agent-platform-secrets` must contain
`AGENT_PLATFORM_DATABASE_URL`; model credentials belong only in `agent-runner-secrets`.

```powershell
kubectl apply -f deploy/kubernetes/namespace.yaml
kubectl -n agent-platform create secret generic agent-platform-secrets --from-literal=AGENT_PLATFORM_DATABASE_URL='<postgresql+psycopg URL>'
kubectl -n agent-platform create secret generic agent-runner-secrets --from-literal=TRAE_API_KEY='<temporary-api-key>'
kubectl apply -f deploy/kubernetes/migrate-job.yaml
kubectl -n agent-platform wait --for=condition=complete job/agent-platform-db-migrate --timeout=5m
kubectl apply -f deploy/kubernetes/platform.yaml
```

`platform.yaml` includes a development Redis Deployment. Replace it with managed Redis for
production if desired; Redis persistence is not part of correctness. Install KEDA first and
then apply `deploy/kubernetes/keda-worker.yaml` to scale Workers from PostgreSQL queue depth.
Without KEDA, keep a fixed Worker replica count. Workspace volumes require a StorageClass that
supports the configured `ReadWriteOnce` PVCs.

## Implemented first-release boundaries

- Workspace, Session and Conversation resources with fixed Session-to-Workspace binding.
- Conversation-scoped ordered event envelopes and SSE replay by `after_seq`.
- Idempotency keys, `expected_seq` conflict checks, approval/input state transitions,
  configurable 30-minute pause worker, and checkpoint hash/lease validation.
- Single active Session container and Workspace write-lease model with FIFO-compatible
  queued state and `429 RESOURCE_EXHAUSTED` when the queue is full.
- Local Workspace provider and a replaceable Docker RuntimeDriver boundary.
- SQLAlchemy PostgreSQL repository for resource projections, event append, idempotency,
  checkpoints and lease records; SQLite is used for repository unit tests.
- PostgreSQL job/outbox/command coordination with `SKIP LOCKED`, advisory-lock idempotency,
  expiring ownership, fencing and independent Worker/Reconciler/Publisher processes.
- Stateless horizontally scalable API replicas, Redis-assisted SSE notifications with durable
  polling fallback, operational readiness and queue/runtime/outbox metrics.
- Kubernetes per-Session Runner Pods, Services and Workspace PVCs, plus Compose and optional
  KEDA deployment topology for independent API and Worker scaling.
- Private Runner HTTP contract: health, run, input, approval, checkpoint, cancel and event replay.
- Deterministic Runner tool batches go through ToolGateway authorization, whole-batch approval,
  result events and call-id deduplication; application-specific handlers are injected by the Runner.
- Authorized local Skills are hashed into a public manifest and mounted read-only at
  `/opt/agent-skills/<skill_id>`; the first MCP adapter exposes only explicitly allowed servers.

## Remaining production boundaries

MinIO/S3/Restic versioning, formal authentication, production MCP/Skill marketplace adapters,
multi-region consensus and managed backup/restore remain outside this release. Compose uses a
shared development Runner; Kubernetes provides the delivered dynamic per-Session RuntimeDriver.
The controlled Trae source, Session image, ToolGateway boundary, trajectory events and checkpoint
replacement/resume path are included.

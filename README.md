# Agent Platform

This repository contains the first implementation slice from `.dev/lab/confirmation/accepted.md`.
It is an API-first Python 3.12 service with a private Session Runner contract.

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

To run the local API against the real WSL2 Docker Driver instead of the deterministic
in-memory runtime, set `AGENT_PLATFORM_RUNTIME_DRIVER=docker_cli` and keep
`AGENT_PLATFORM_RUNTIME_CONTEXT=desktop-linux`.

Run the private runner:

```powershell
.venv\Scripts\python.exe -m uvicorn session_runner.main:app --port 8080
```

## WSL2 Docker development

This project is verified with Docker Desktop's `desktop-linux` WSL2 context. From
PowerShell or a WSL shell, use the context explicitly when the Windows named-pipe
`default` context is unavailable:

```powershell
docker context use desktop-linux
docker compose up -d --build
docker compose ps
Invoke-WebRequest -UseBasicParsing http://localhost:8000/live
```

If the WSL backend has stopped, start Docker Desktop from the approved installation
path and wait for the server to respond before running Compose:

```powershell
Start-Process -FilePath 'E:\Docker\Docker Desktop.exe'
docker --context desktop-linux version
```

Compose starts the Session Runner in real Trae mode. Set `TRAE_API_KEY` and, when
needed, `TRAE_MODEL_BASE_URL` in the environment before starting the stack; secrets
are passed to the Runner process only and are never written to the YAML config or image.
The vendored MIT source snapshot is under `vendor/trae-agent-src` and is invoked only
through Trae's `_tool_caller` boundary.

For an OpenAI-compatible provider, override the provider and model in the same shell:

```powershell
$env:TRAE_PROVIDER = "openai"
$env:TRAE_API_KEY = "<temporary-api-key>"
$env:TRAE_MODEL_BASE_URL = "https://example.com/compatible-mode/v1"
$env:TRAE_MODEL = "<model-name>"
docker --context desktop-linux compose up -d --force-recreate --no-build
```

For rapid source-only iterations after the first successful image build, reuse the
existing dependency image and avoid pip index downloads:

```powershell
docker --context desktop-linux build --build-arg BASE_IMAGE=agentsupport-api:latest --build-arg INSTALL_DEPS=false -t agentsupport-api:latest .
docker --context desktop-linux compose up -d --force-recreate --no-build
```

The verified environment uses Docker Desktop 29.6.2, WSL2, PostgreSQL 16 and the
`agent-session:dev` image. The Compose API is exposed at `http://localhost:8000` and
PostgreSQL at `localhost:5432`.

The temporary acceptance console is served by the API at
`http://localhost:8000/debug/`. It creates the Workspace, Session and Conversation
through public platform routes and exposes event replay, SSE status, approvals, input
and cancellation. It never accepts or stores model credentials.

`docker compose up --build` starts the API container and PostgreSQL in
`AGENT_PLATFORM_PERSISTENCE_MODE=postgres` mode. The API runs the explicit schema
initializer before startup, and resources/events survive an API container restart.
Local Python execution defaults to the in-memory repository for deterministic tests;
set `AGENT_PLATFORM_PERSISTENCE_MODE=postgres` when running against PostgreSQL.

After PostgreSQL is available, initialize the explicit schema with:

```powershell
.venv\Scripts\python.exe -m agent_platform.db
```

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
- Private Runner HTTP contract: health, run, input, approval, checkpoint, cancel and event replay.
- Deterministic Runner tool batches go through ToolGateway authorization, whole-batch approval,
  result events and call-id deduplication; application-specific handlers are injected by the Runner.
- Authorized local Skills are hashed into a public manifest and mounted read-only at
  `/opt/agent-skills/<skill_id>`; the first MCP adapter exposes only explicitly allowed servers.

## Deliberately not delivered yet

Redis, Kubernetes, MinIO/S3/Restic versioning, formal authentication, production MCP/Skill
marketplace adapters, and a real Docker socket/container reconciliation driver remain later
stages explicitly listed in the accepted plan. The controlled Trae source, Session image,
ToolGateway boundary, trajectory events, and checkpoint resume path are included in this release.

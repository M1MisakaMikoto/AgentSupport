# Distributed Dynamic Scaling Implementation Plan

Status: completed
Date: 2026-07-28

## Objective

Evolve the current single-host API/Runner/PostgreSQL implementation into a service that can
run multiple API and control-plane replicas, dynamically scale execution capacity, survive
individual process or Runner loss, and preserve the single-writer Workspace invariant.

## Non-negotiable invariants

1. PostgreSQL is the authoritative store for resources, execution state, events, commands,
   checkpoints, ownership, leases, and Runner routing.
2. Redis is an optional notification accelerator. Redis loss may delay wake-ups but may not lose
   accepted work or events.
3. API processes are stateless. They do not schedule containers or own background supervision.
4. Work delivery is at-least-once. Idempotency keys, event identity, claim tokens, and monotonically
   increasing fencing epochs prevent duplicate externally visible effects.
5. At most one valid writer may hold a Workspace lease. Every Runner control request carries the
   current lease and fence epoch.
6. No Docker or Kubernetes API call occurs while a database transaction is held open.
7. A Runner may be replaced. A replacement resumes from the latest validated checkpoint or ends
   the Run as LOST when safe recovery is impossible.

## Target topology

```text
Load balancer
    |
API replicas ----- PostgreSQL ----- scheduler/worker replicas
    |                   |                       |
    +-- event notifier -+                 RuntimeDriver
                                                |
                                  Docker container or Kubernetes Pod
                                                |
                                       Workspace volume
```

## Delivery stages

### Stage 1: durable coordination

- Add schema-managed execution jobs, runtime slots, Runner endpoints, durable commands, outbox
  notifications, and expiring Workspace/container leases.
- Add repository operations that claim jobs with `FOR UPDATE SKIP LOCKED`, renew ownership, and
  apply state transitions with a claim-token/fence precondition.
- Make event append and Conversation projection updates one transaction.
- Keep SQLite-compatible paths for fast unit tests, but prove claim concurrency against PostgreSQL.

### Stage 2: stateless API and independent control plane

- Stop hydrating long-lived resource dictionaries in PostgreSQL mode.
- Make API reads use repository queries and writes use atomic repository application operations.
- Move queue draining, waiting-input expiry, runtime supervision, and reconciliation out of the
  FastAPI lifespan into independently runnable Worker and Reconciler processes.
- Preserve the in-memory adapter for deterministic unit tests only.

### Stage 3: distributed events and recoverable Runner routing

- Add EventNotifier and RunnerDirectory ports.
- Implement database replay plus local notification first; add Redis notification as an optional
  adapter without making it authoritative.
- Persist Runner endpoint, runtime identity, lease epoch, health heartbeat, and the last imported
  Runner sequence.
- Persist input/approval/cancel commands. The current owner delivers commands idempotently.
- Reconcile missed Runner events using `/runs/{run_id}/events?after_seq=`.

### Stage 4: dynamic execution capacity

- Extend RuntimeDriver with idempotent desired-state semantics and instance discovery.
- Retain DockerCliRuntimeDriver for shared-daemon multi-worker development.
- Add KubernetesRuntimeDriver that creates one fenced Runner workload per active Session, attaches
  the Workspace PVC, exposes a private endpoint, and discovers/removes orphaned workloads.
- Scale control-plane workers horizontally from queue depth. Scale Runner capacity by creating and
  deleting per-Session workloads; do not load-balance stateful Run RPCs across arbitrary Runners.

### Stage 5: deployment and operations

- Add separate API, Worker, Reconciler, PostgreSQL, and optional Redis services to Compose.
- Add Kubernetes manifests for API/Worker/Reconciler Deployments, RBAC, Services, NetworkPolicy,
  configuration, and an autoscaling policy.
- Add readiness checks that cover dependency availability without coupling liveness to PostgreSQL.
- Add metrics for queue depth, oldest ready job, claim expiry, reconciliation, Runner startup,
  Workspace lease contention, and event publication lag.

## Planned code changes

| Area | Existing files | New files |
| --- | --- | --- |
| Schema | `agentsupport/db.py` | `alembic/*` |
| Coordination | `agentsupport/repository.py` | `agentsupport/jobs.py` |
| API/service | `agentsupport/api.py`, `agentsupport/services.py` | - |
| Control plane | - | `agentsupport/worker.py`, `agentsupport/reconciler.py` |
| Events | `agentsupport/events.py` | `agentsupport/event_notifier.py` |
| Runtime | `agentsupport/runtime.py`, `agentsupport/ports.py` | `agentsupport/kubernetes_runtime.py` |
| Deployment | `docker-compose.yml`, `Dockerfile` | `deploy/kubernetes/*` |

## Rollout

1. Deploy additive schema changes while the legacy scheduler is still active.
2. Deploy code with distributed coordination disabled and verify legacy behavior.
3. Enable durable event reads and API stateless mode.
4. Start one Worker, disable the API lifespan workers, and drain existing queued work.
5. Increase Worker and API replicas; validate claim/fence behavior under forced process loss.
6. Enable Redis notifications, then Kubernetes RuntimeDriver, independently.
7. Remove compatibility switches only after the rollback window closes.

## Required verification

- Full fast test suite passes with a workspace-owned pytest temporary directory.
- Two service instances observe each other's writes without restart.
- Concurrent workers claim each job once at a time and stale claim tokens cannot mutate state.
- Expired claims are recoverable and retry limits are enforced.
- Concurrent event append preserves strictly increasing Conversation sequence numbers.
- Redis/notifier outage does not prevent database replay.
- Runner loss is detected, stale endpoints are fenced, and checkpoint recovery is exercised.
- Dynamic capacity grows with runnable Sessions and shrinks after terminal completion.
- Only one valid writer exists for a Workspace throughout failover.
- Compose integration and Kubernetes manifest validation pass.

## Completion rule

The project may claim distributed dynamic scaling support only after all required verification is
backed by executable tests or runtime evidence. Passing legacy single-process tests alone is not
sufficient.

## Acceptance evidence

Completed on 2026-07-28.

- Fast suite: `69 passed, 7 skipped`; Ruff passed with no findings.
- PostgreSQL concurrency suite: `3 passed`, covering independent `SKIP LOCKED` claims,
  the single-writer Workspace invariant, advisory-lock idempotency, and concurrent event sequence
  allocation.
- Retry/failover tests cover expired claims, stale-token fencing, maximum attempts, missed runtime
  recovery, notifier failure polling, and replacement-Runner checkpoint restoration.
- Alembic was verified both from an empty database and incrementally from revision `0001`; SQLite
  and the live Compose PostgreSQL database reached `20260728_0003`.
- Compose ran 2 API replicas and 3 Worker replicas. Nginx returned two distinct
  `X-AgentSupport-Instance` values. A capacity test observed 2 waiting jobs plus 1 queued job, promoted
  the queued job after release, completed all 3, and returned active runtime count to zero.
- A waiting Run was paused by the Reconciler, its Runner container was replaced, and input moved
  it through `RESUMING`, `checkpoint.restored`, and `run.completed`.
- Redis Pub/Sub carried the durable Outbox event envelopes. With Redis stopped, a new Run still
  completed and replayed all events from PostgreSQL while 6 Outbox records remained pending; the
  backlog drained to zero after Redis recovery.
- `docker compose config` passed. All 17 Kubernetes YAML objects parsed, and the mock Kubernetes
  contract verifies PVC/Service/Pod creation, fence injection, readiness, and cleanup. No cluster
  API was configured locally, so server-side admission remains a deployment-environment check.

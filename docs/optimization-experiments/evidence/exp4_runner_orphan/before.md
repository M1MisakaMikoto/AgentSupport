# Experiment `exp4_runner_orphan` — phase `before`

- timestamp: `2026-08-25T05:09:41+00:00`
- git head: `307c773`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp4_runner_orphan.py --phase before`

---

## Orphan-run reproduction (fake agent sleeps 3s, control plane timeout 0.5s)
| observation | value |
| --- | --- |
| control plane conversation state after POST | FAILED |
| control plane run.error | {'code': 'CORE_RUNTIME_ERROR', 'message': ''} |
| control plane request wall time | 0.98s |
| runner events at +0.5s (timeout) | run.started |
| runner events at +3.7s (agent finished) | run.started, trajectory.missing, message, run.completed |
| runner emitted run.completed after control plane gave up | True |
| runner emitted run.cancelled | False |

### event timeline
```text
t=0.00s  control plane POST /runs
t=0.50s  control plane httpx timeout -> conversation FAILED
t=3.00s  fake agent finishes
t=3.70s  runner state read for evidence
```

### runner events after the wait
```json
[
  {
    "schema_version": "1",
    "event_id": "d4dc521e-e221-4a42-bccf-fe7e3ec154c0",
    "run_id": "009350b9-44b5-4304-a8c9-03a5a640fb59",
    "seq": 1,
    "type": "run.started",
    "payload": {
      "conversation_id": "3b8b64fa-b925-460c-bbf6-9fdd24740e5a"
    },
    "tenant_id": null,
    "user_id": null,
    "project_id": null,
    "source": "session_runner",
    "occurred_at": "2026-08-25T05:09:36.813463Z"
  },
  {
    "schema_version": "1",
    "event_id": "0c79d7b1-f930-4990-8ea8-1bb377513d6c",
    "run_id": "009350b9-44b5-4304-a8c9-03a5a640fb59",
    "seq": 2,
    "type": "trajectory.missing",
    "payload": {
      "path": "D:\\workspace\\.agentsupport\\trajectories\\009350b9-44b5-4304-a8c9-03a5a640fb59.json"
    },
    "tenant_id": null,
    "user_id": null,
    "project_id": null,
    "source": "session_runner",
    "occurred_at": "2026-08-25T05:09:39.830989Z"
  },
  {
    "schema_version": "1",
    "event_id": "e90c8d43-b5ca-41ff-bf03-5c07f77e1104",
    "run_id": "009350b9-44b5-4304-a8c9-03a5a640fb59",
    "seq": 3,
    "type": "message",
    "payload": {
      "content": "done-after-timeout"
    },
    "tenant_id": null,
    "user_id": null,
    "project_id": null,
    "source": "session_runner",
    "occurred_at": "2026-08-25T05:09:39.830989Z"
  },
  {
    "schema_version": "1",
    "event_id": "3044a282-c2d5-4cfb-adee-b08afe6bbd34",
    "run_id": "009350b9-44b5-4304-a8c9-03a5a640fb59",
    "seq": 4,
    "type": "run.completed",
    "payload": {
      "result": {
        "status": "completed",
        "content": "done-after-timeout",
        "steps": 0
      }
    },
    "tenant_id": null,
    "user_id": null,
    "project_id": null,
    "source": "session_runner",
    "occurred_at": "2026-08-25T05:09:39.830989Z"
  }
]
```

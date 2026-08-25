# Experiment `exp4_runner_orphan` — phase `after`

- timestamp: `2026-08-25T07:00:05+00:00`
- git head: `2944874`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp4_runner_orphan.py --phase after`

---

## Orphan-run reproduction (fake agent sleeps 3s, control plane timeout 0.5s)
| observation | value |
| --- | --- |
| control plane conversation state after POST | FAILED |
| control plane run.error | {'code': 'CORE_RUNTIME_ERROR', 'message': ''} |
| control plane request wall time | 1.17s |
| runner events at +0.5s (timeout) | run.started, run.cancelled |
| runner events at +3.7s (agent finished) | run.started, run.cancelled |
| runner emitted run.completed after control plane gave up | False |
| runner emitted run.cancelled | True |

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
    "event_id": "6e627fdd-9dc3-40d6-9ade-cd59ad2d8a92",
    "run_id": "85982dd1-f11b-4fb4-a2c5-829b53a57f5d",
    "seq": 1,
    "type": "run.started",
    "payload": {
      "conversation_id": "8534f142-8e50-4402-8355-0859d06d41d9"
    },
    "tenant_id": null,
    "user_id": null,
    "project_id": null,
    "source": "session_runner",
    "occurred_at": "2026-08-25T07:00:00.999811Z"
  },
  {
    "schema_version": "1",
    "event_id": "cdc5631d-0f78-4984-adef-6466f7aee234",
    "run_id": "85982dd1-f11b-4fb4-a2c5-829b53a57f5d",
    "seq": 2,
    "type": "run.cancelled",
    "payload": {},
    "tenant_id": null,
    "user_id": null,
    "project_id": null,
    "source": "session_runner",
    "occurred_at": "2026-08-25T07:00:01.512610Z"
  }
]
```

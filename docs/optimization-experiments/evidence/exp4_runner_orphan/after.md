# Experiment `exp4_runner_orphan` — phase `after`

- timestamp: `2026-08-25T05:16:13+00:00`
- git head: `307c773`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp4_runner_orphan.py --phase after`

---

## Orphan-run reproduction (fake agent sleeps 3s, control plane timeout 0.5s)
| observation | value |
| --- | --- |
| control plane conversation state after POST | FAILED |
| control plane run.error | {'code': 'CORE_RUNTIME_ERROR', 'message': ''} |
| control plane request wall time | 1.48s |
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
    "event_id": "e02a1c04-e842-4459-84a4-bf7d12292e82",
    "run_id": "04b27f18-f3c1-4ef9-a462-d7dcac83e415",
    "seq": 1,
    "type": "run.started",
    "payload": {
      "conversation_id": "004c7049-1b4b-4e4a-8614-7b7a267f653f"
    },
    "tenant_id": null,
    "user_id": null,
    "project_id": null,
    "source": "session_runner",
    "occurred_at": "2026-08-25T05:16:07.922770Z"
  },
  {
    "schema_version": "1",
    "event_id": "fb327968-6ac6-4c78-83d5-f1c7c6f97676",
    "run_id": "04b27f18-f3c1-4ef9-a462-d7dcac83e415",
    "seq": 2,
    "type": "run.cancelled",
    "payload": {},
    "tenant_id": null,
    "user_id": null,
    "project_id": null,
    "source": "session_runner",
    "occurred_at": "2026-08-25T05:16:08.952408Z"
  }
]
```

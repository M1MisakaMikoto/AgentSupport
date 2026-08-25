# Experiment `exp3_publish_tasks` — phase `after`

- timestamp: `2026-08-25T06:59:56+00:00`
- git head: `2944874`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp3_publish_tasks.py --phase after`

---

### (a) slow publish, 30 appends

- publish calls at +20ms: 30/30
- publish tasks still in flight at +20ms: 30
- asyncio tasks created by the event loop at +0ms: 30
- tracked publish tasks on the service: 30


### (a.2) after all publishes finish

- tracked publish tasks on the service: 0


### (b) failing publish

- asyncio 'Task exception was never retrieved' records: 0
  ```

  ```
- application-level error records: 1
  ```
event publish task failed (Task-32): probe publish failure
  ```

# Experiment `exp3_publish_tasks` — phase `before`

- timestamp: `2026-08-25T05:04:19+00:00`
- git head: `307c773`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp3_publish_tasks.py --phase before`

---

### (a) slow publish, 30 appends

- publish calls at +20ms: 30/30
- publish tasks still in flight at +20ms: 30
- asyncio tasks created by the event loop at +0ms: 30
- tracked publish tasks on the service: <missing attribute _publish_tasks>


### (b) failing publish

- asyncio 'Task exception was never retrieved' records: 1
  ```
Task exception was never retrieved
future: <Task finished name='Task-32' coro=<ProbeStore.publish() done, defined at D:\dev\projects\AgentSupport\devtools\experiments\exp3_publish_tasks.py:39> exception=RuntimeError('probe publish failure')>
  ```
- application-level error records: 1
  ```
Task exception was never retrieved
future: <Task finished name='Task-32' coro=<ProbeStore.publish() done, defined at D:\dev\projects\AgentSupport\devtools\experiments\exp3_publish_tasks.py:39> exception=RuntimeError('probe publish failure')>
  ```

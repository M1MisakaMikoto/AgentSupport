# Experiment `exp20_metrics_temporal_truth` — phase `before`

- timestamp: `2026-08-25T06:26:34+00:00`
- git head: `307c773`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp20_metrics_temporal_truth.py --phase before`

---

## Coordination metrics vs database truth (temporal mode)
| observation | value |
| --- | --- |
| QUEUED conversations in DB | 5 |
| RUNNING conversations in DB | 1 |
| WAITING_INPUT conversations in DB | 1 |
| ACTIVE container leases in DB | 1 |
| agentsupport_queue_ready | 0.0 |
| agentsupport_jobs_running | 0.0 |
| agentsupport_jobs_waiting | 0.0 |
| agentsupport_active_runtimes | 0.0 |
| agentsupport_queue_oldest_ready_seconds | 0.0 |

Before the fix the gauges read 0 because they are computed from the
in-memory maps that are empty in temporal mode. After the fix the
gauges are computed from PostgreSQL.

# Experiment `exp6_outbox_metrics` — phase `after`

- timestamp: `2026-08-25T05:16:31+00:00`
- git head: `307c773`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp6_outbox_metrics.py --phase after`

---

## Outbox metrics from `GET /metrics`
| observation | value |
| --- | --- |
| unpublished outbox rows seeded | 6 |
| agentsupport_outbox_pending scrape value | 6.0 |
| oldest pending row age | 2h |
| agentsupport_outbox_publication_lag_seconds scrape value | 7200.114 |

Expected before fix: scrape values are `0.0` despite 6 pending rows.
Expected after fix: `agentsupport_outbox_pending = 6.0` and lag > 0.

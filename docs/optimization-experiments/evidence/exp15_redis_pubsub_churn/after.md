# Experiment `exp15_redis_pubsub_churn` — phase `after`

- timestamp: `2026-08-25T06:04:03+00:00`
- git head: `307c773`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp15_redis_pubsub_churn.py --phase after`

---

## Redis pubsub churn per SSE wait
| observation | value |
| --- | --- |
| wait() calls | 30 |
| pubsub objects created | 1 |
| SUBSCRIBE commands issued | 1 |
| publish wakes a waiter (functional) | True |

Before the fix every wait() creates a new pubsub subscription
(one SUBSCRIBE + close per poll). After the fix one subscription
per channel is reused across waits.

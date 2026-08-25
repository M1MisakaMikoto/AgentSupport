# Experiment `exp12_rest_route_blocking` — phase `before`

- timestamp: `2026-08-25T05:45:13+00:00`
- git head: `307c773`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp12_rest_route_blocking.py --phase before`

---

## Event-loop stall by sync REST handlers
| observation | value |
| --- | --- |
| sessions in database | 40000 |
| GET /sessions status | 200 |
| GET /sessions duration | 5050 ms |
| canary timer scheduled for +100ms fired at | 5047 ms |
| event-loop stall (canary overrun) | 4947 ms |

Before the fix the async handler blocks the loop for the whole
request. After converting non-awaiting handlers to plain `def`
(FastAPI worker thread), the canary fires on schedule.

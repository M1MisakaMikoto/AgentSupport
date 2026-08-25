# Experiment `exp13_sse_keepalive` — phase `before`

- timestamp: `2026-08-25T05:49:09+00:00`
- git head: `307c773`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp13_sse_keepalive.py --phase before`

---

## SSE idle keepalive
| observation | value |
| --- | --- |
| configured keepalive interval | 1s |
| time to first byte | 3062 ms |
| first chunk content | (no data) |

Before the fix an idle stream sends nothing (first byte never
arrives within a 3s probe window). After the fix a keepalive
comment frame arrives at the configured interval.

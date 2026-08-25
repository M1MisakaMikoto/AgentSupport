# Experiment `exp13_sse_keepalive` — phase `after`

- timestamp: `2026-08-25T07:02:00+00:00`
- git head: `2944874`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp13_sse_keepalive.py --phase after`

---

## SSE idle keepalive
| observation | value |
| --- | --- |
| configured keepalive interval | 1s |
| time to first byte | 1094 ms |
| first chunk content | : keepalive |

Before the fix an idle stream sends nothing (first byte never
arrives within a 3s probe window). After the fix a keepalive
comment frame arrives at the configured interval.

# Experiment `exp19_sse_last_event_id` — phase `before`

- timestamp: `2026-08-25T06:25:40+00:00`
- git head: `307c773`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp19_sse_last_event_id.py --phase before`

---

## SSE Last-Event-ID resume
| observation | value |
| --- | --- |
| events in conversation | 3 |
| request header Last-Event-ID | 2 |
| first delivered frame id(s) | id: 1 |

Before the fix the header is ignored and the stream replays from
seq 1 (first frame id: 1). After the fix the stream resumes after
seq 2 (first frame id: 3).

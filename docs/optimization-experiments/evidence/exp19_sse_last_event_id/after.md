# Experiment `exp19_sse_last_event_id` — phase `after`

- timestamp: `2026-08-25T06:27:59+00:00`
- git head: `307c773`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp19_sse_last_event_id.py --phase after`

---

## SSE Last-Event-ID resume
| observation | value |
| --- | --- |
| events in conversation | 3 |
| request header Last-Event-ID | 2 |
| first delivered frame id(s) | id: 3 |

Before the fix the header is ignored and the stream replays from
seq 1 (first frame id: 1). After the fix the stream resumes after
seq 2 (first frame id: 3).

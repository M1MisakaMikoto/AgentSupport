# Experiment `exp9_event_loop_blocking` — phase `after`

- timestamp: `2026-08-25T07:01:23+00:00`
- git head: `2944874`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp9_event_loop_blocking.py --phase after`

---

## Event-loop stall by sync DB reads in SSE streams
| observation | value |
| --- | --- |
| events in conversation | 80000 |
| stream first-poll duration (sync query) | 5452 ms |
| timer scheduled for +100ms actually fired at | 99 ms |
| event-loop stall (timer overrun) | -1 ms |

A timer scheduled for +100ms is the canary: if the sync query holds
the loop, the canary fires only after the query returns (stall ≈ query
duration). After the fix (reads via `asyncio.to_thread`) the canary
fires on schedule while the query runs in a worker thread.

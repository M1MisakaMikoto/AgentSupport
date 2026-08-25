# Experiment `exp18_event_store_lost_wakeup` — phase `before`

- timestamp: `2026-08-25T06:15:18+00:00`
- git head: `307c773`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp18_event_store_lost_wakeup.py --phase before`

---

## In-memory event stream lost-wakeup
| observation | value |
| --- | --- |
| events appended | 1 |
| events delivered by stream | 0 |
| delivered event types | [] |

Before the fix the stream misses the notification (it was not yet
waiting) and never re-checks, so the appended event is not
delivered. After the fix the stream re-checks under the lock and
delivers the event.

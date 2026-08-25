# Experiment `exp17_httpx_client_reuse` — phase `after`

- timestamp: `2026-08-25T07:02:23+00:00`
- git head: `2944874`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp17_httpx_client_reuse.py --phase after`

---

## Core-runtime HTTP client reuse
| observation | value |
| --- | --- |
| HTTP calls made | 5 |
| httpx.AsyncClient instances created | 1 |

Before the fix every call constructs a new AsyncClient (new
connection pool). After the fix one client is reused.

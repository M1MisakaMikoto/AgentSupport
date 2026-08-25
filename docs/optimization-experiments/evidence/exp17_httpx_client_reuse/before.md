# Experiment `exp17_httpx_client_reuse` — phase `before`

- timestamp: `2026-08-25T06:10:08+00:00`
- git head: `307c773`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp17_httpx_client_reuse.py --phase before`

---

## Core-runtime HTTP client reuse
| observation | value |
| --- | --- |
| HTTP calls made | 5 |
| httpx.AsyncClient instances created | 5 |

Before the fix every call constructs a new AsyncClient (new
connection pool). After the fix one client is reused.

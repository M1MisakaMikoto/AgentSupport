# Experiment `exp10_request_size_limits` — phase `before`

- timestamp: `2026-08-25T05:39:48+00:00`
- git head: `307c773`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp10_request_size_limits.py --phase before`

---

## Unbounded request bodies
| observation | value |
| --- | --- |
| POST /sessions/{id}/conversations with 5 MiB task | 201 |
| stored task size (octet_length) | 5242880 |
| POST /sessions with 5 MiB metadata | 201 |
| stored metadata size (octet_length) | 5242892 |

Before the fix a 5 MiB body is accepted and stored. After the fix
the API rejects it with 422 and nothing is written.

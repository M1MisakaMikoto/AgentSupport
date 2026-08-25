# Experiment `exp10_request_size_limits` — phase `after`

- timestamp: `2026-08-25T07:01:29+00:00`
- git head: `2944874`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp10_request_size_limits.py --phase after`

---

## Unbounded request bodies
| observation | value |
| --- | --- |
| POST /sessions/{id}/conversations with 5 MiB task | 422 |
| stored task size (octet_length) | 0 |
| POST /sessions with 5 MiB metadata | 422 |
| stored metadata size (octet_length) | 0 |

Before the fix a 5 MiB body is accepted and stored. After the fix
the API rejects it with 422 and nothing is written.

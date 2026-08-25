# Experiment `exp14_list_pagination` — phase `after`

- timestamp: `2026-08-25T07:04:15+00:00`
- git head: `2944874`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp14_list_pagination.py --phase after`

---

## List endpoint pagination
| observation | value |
| --- | --- |
| sessions in database | 40000 |
| GET /sessions?limit=100 -> items returned | 100 |
| GET /sessions?limit=100 -> duration | 163 ms |
| GET /sessions?limit=100 -> response bytes | 28501 |
| GET /sessions (no params) -> items returned | 40000 |
| GET /sessions (no params) -> duration | 6067 ms |
| GET /sessions (no params) -> response bytes | 11400001 |

Before the fix the client's `limit=100` is silently ignored and the
full table is returned. After the fix `limit`/`offset` are honored.

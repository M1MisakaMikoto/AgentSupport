# Experiment `exp21_list_default_limit` — phase `after`

- timestamp: `2026-08-25T07:02:57+00:00`
- git head: `2944874`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp21_list_default_limit.py --phase after`

---

## List endpoint default limit
| observation | value |
| --- | --- |
| sessions in database | 40000 |
| GET /sessions (no params) -> items | 100 |
| GET /sessions (no params) -> bytes | 28501 |
| GET /sessions?limit=1000 -> items | 1000 |

Before the fix a no-parameter request returns the full table.
After the fix it returns at most the configured default limit
(`AGENTSUPPORT_LIST_DEFAULT_LIMIT`, default 100) while an explicit
`limit` still overrides.

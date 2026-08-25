# Experiment `exp16_runner_load_balancing` — phase `after`

- timestamp: `2026-08-25T07:03:54+00:00`
- git head: `2944874`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp16_runner_load_balancing.py --phase after`

---

## Runner selection vs advertised load
| observation | value |
| --- | --- |
| Runner A advertised load | 5 |
| Runner B advertised load | 2 |
| Runner C advertised load | 0 |
| selection count -> A | 0 |
| selection count -> B | 0 |
| selection count -> C | 60 |

Before the fix the first-registered Runner is always picked even
with load 5. After the fix the lowest-load Runner is preferred.

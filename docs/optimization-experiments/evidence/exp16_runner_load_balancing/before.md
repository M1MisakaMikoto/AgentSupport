# Experiment `exp16_runner_load_balancing` — phase `before`

- timestamp: `2026-08-25T06:09:26+00:00`
- git head: `307c773`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp16_runner_load_balancing.py --phase before`

---

## Runner selection vs advertised load
| observation | value |
| --- | --- |
| Runner A advertised load | 5 |
| Runner B advertised load | 2 |
| Runner C advertised load | 0 |
| selection count -> A | 60 |
| selection count -> B | 0 |
| selection count -> C | 0 |

Before the fix the first-registered Runner is always picked even
with load 5. After the fix the lowest-load Runner is preferred.

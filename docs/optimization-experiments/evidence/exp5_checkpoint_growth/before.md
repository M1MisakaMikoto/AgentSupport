# Experiment `exp5_checkpoint_growth` — phase `before`

- timestamp: `2026-08-25T05:09:51+00:00`
- git head: `307c773`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp5_checkpoint_growth.py --phase before`

---

## Checkpoint payload vs event count
| tool calls | run events | recent_events stored | recent_events size | full checkpoint size |
| --- | --- | --- | --- | --- |
| 200 | 203 | 203 | 77.2 KiB | 118.9 KiB |
| 800 | 803 | 803 | 306.3 KiB | 471.1 KiB |

Expected before fix: `recent_events stored == run events` and size grows
linearly with history. Expected after fix: `recent_events stored` is
capped to a bounded window while `run events` still grows.

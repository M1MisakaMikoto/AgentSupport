# Experiment `exp5_checkpoint_growth` — phase `after`

- timestamp: `2026-08-25T07:00:08+00:00`
- git head: `2944874`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp5_checkpoint_growth.py --phase after`

---

## Checkpoint payload vs event count
| tool calls | run events | recent_events stored | recent_events size | full checkpoint size |
| --- | --- | --- | --- | --- |
| 200 | 203 | 200 | 76.0 KiB | 117.8 KiB |
| 800 | 803 | 200 | 76.3 KiB | 241.1 KiB |

Expected before fix: `recent_events stored == run events` and size grows
linearly with history. Expected after fix: `recent_events stored` is
capped to a bounded window while `run events` still grows.

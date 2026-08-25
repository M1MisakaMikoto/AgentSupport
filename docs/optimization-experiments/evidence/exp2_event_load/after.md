# Experiment `exp2_event_load` — phase `after`

- timestamp: `2026-08-25T05:15:22+00:00`
- git head: `307c773`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp2_event_load.py --phase after`

---

## Warm-up cost vs event count (min of 3 runs)
| events (E) | warm-up time | events loaded |
| --- | --- | --- |
| 500 | 30.16 ms | 500 |
| 1000 | 45.31 ms | 1000 |
| 2000 | 83.51 ms | 2000 |
| 4000 | 146.09 ms | 4000 |

Scaling diagnosis (`t/E` should stay flat for O(E), grow for O(E²)):

- E=500: t/E = 60.33 µs/event
- E=1000: t/E = 45.31 µs/event
- E=2000: t/E = 41.75 µs/event
- E=4000: t/E = 36.52 µs/event

```text
O(E^2) expected when the fill loop checks events_store.list() per event;
O(E) expected when the fill loop appends missing events in one pass.
```

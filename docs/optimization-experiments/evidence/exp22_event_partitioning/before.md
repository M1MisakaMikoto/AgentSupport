# Experiment `exp22_event_partitioning` — phase `before`

- timestamp: `2026-08-25T06:43:42+00:00`
- git head: `fa45b14`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp22_event_partitioning.py --phase before`

---

## Monthly partitioning of conversation_events
| observation | value |
| --- | --- |
| table is partitioned | plain table |
| month partitions | (none) |
| retention DELETE plan head | Delete on conversation_events |

### retention DELETE plan
```text
Delete on conversation_events
  ->  Nested Loop
        ->  Seq Scan on conversations
              Filter: ((execution_state)::text = ANY ('{COMPLETED,FAILED,CANCELLED,LOST}'::text[]))
        ->  Bitmap Heap Scan on conversation_events
              Recheck Cond: ((conversation_id)::text = (conversations.id)::text)
              Filter: (occurred_at < '2026-09-01 00:00:00+00'::timestamp with time zone)
              ->  Bitmap Index Scan on ix_conversation_events_conversation_id
                    Index Cond: ((conversation_id)::text = (conversations.id)::text)
```

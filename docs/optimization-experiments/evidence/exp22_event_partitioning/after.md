# Experiment `exp22_event_partitioning` — phase `after`

- timestamp: `2026-08-25T07:07:11+00:00`
- git head: `2944874`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp22_event_partitioning.py --phase after`

---

## Monthly partitioning of conversation_events
| observation | value |
| --- | --- |
| table is partitioned | p |
| month partitions | conversation_events_202601, conversation_events_202602, conversation_events_202603, conversation_events_202604, conversation_events_202606, conversation_events_202607, conversation_events_202608, conversation_events_202609, conversation_events_202610, conversation_events_202611 |
| retention DELETE plan head | Delete on conversation_events |
| partition-scoped Delete nodes in plan | 7 |
| automatic partition after write (2026-12) | True |
| idle partitions dropped before 2026-05 | 4 |
| retained partitions | conversation_events_202606, conversation_events_202607, conversation_events_202608, conversation_events_202609, conversation_events_202610, conversation_events_202611, conversation_events_202612 |

### retention DELETE plan
```text
Delete on conversation_events
  Delete on conversation_events_202601 conversation_events_1
  Delete on conversation_events_202602 conversation_events_2
  Delete on conversation_events_202603 conversation_events_3
  Delete on conversation_events_202604 conversation_events_4
  Delete on conversation_events_202606 conversation_events_5
  Delete on conversation_events_202607 conversation_events_6
  Delete on conversation_events_202608 conversation_events_7
  ->  Nested Loop
        ->  Seq Scan on conversations
              Filter: ((execution_state)::text = ANY ('{COMPLETED,FAILED,CANCELLED,LOST}'::text[]))
        ->  Append
              ->  Bitmap Heap Scan on conversation_events_202601 conversation_events_1
                    Recheck Cond: ((conversation_id)::text = (conversations.id)::text)
                    Filter: (occurred_at < '2026-09-01 00:00:00+00'::timestamp with time zone)
                    ->  Bitmap Index Scan on ix_conversation_events_202601_conversation_id
                          Index Cond: ((conversation_id)::text = (conversations.id)::text)
              ->  Bitmap Heap Scan on conversation_events_202602 conversation_events_2
                    Recheck Cond: ((conversation_id)::text = (conversations.id)::text)
                    Filter: (occurred_at < '2026-09-01 00:00:00+00'::timestamp with time zone)
                    ->  Bitmap Index Scan on ix_conversation_events_202602_conversation_id
                          Index Cond: ((conversation_id)::text = (conversations.id)::text)
              ->  Bitmap Heap Scan on conversation_events_202603 conversation_events_3
                    Recheck Cond: ((conversation_id)::text = (conversations.id)::text)
                    Filter: (occurred_at < '2026-09-01 00:00:00+00'::timestamp with time zone)
                    ->  Bitmap Index Scan on ix_conversation_events_202603_conversation_id
                          Index Cond: ((conversation_id)::text = (conversations.id)::text)
              ->  Bitmap Heap Scan on conversation_events_202604 conversation_events_4
                    Recheck Cond: ((conversation_id)::text = (conversations.id)::text)
                    Filter: (occurred_at < '2026-09-01 00:00:00+00'::timestamp with time zone)
                    ->  Bitmap Index Scan on ix_conversation_events_202604_conversation_id
                          Index Cond: ((conversation_id)::text = (conversations.id)::text)
              ->  Bitmap Heap Scan on conversation_events_202606 conversation_events_5
                    Recheck Cond: ((conversation_id)::text = (conversations.id)::text)
                    Filter: (occurred_at < '2026-09-01 00:00:00+00'::timestamp with time zone)
                    ->  Bitmap Index Scan on ix_conversation_events_202606_conversation_id
                          Index Cond: ((conversation_id)::text = (conversations.id)::text)
              ->  Bitmap Heap Scan on conversation_events_202607 conversation_events_6
                    Recheck Cond: ((conversation_id)::text = (conversations.id)::text)
                    Filter: (occurred_at < '2026-09-01 00:00:00+00'::timestamp with time zone)
                    ->  Bitmap Index Scan on ix_conversation_events_202607_conversation_id
                          Index Cond: ((conversation_id)::text = (conversations.id)::text)
              ->  Bitmap Heap Scan on conversation_events_202608 conversation_events_7
                    Recheck Cond: ((conversation_id)::text = (conversations.id)::text)
                    Filter: (occurred_at < '2026-09-01 00:00:00+00'::timestamp with time zone)
                    ->  Bitmap Index Scan on ix_conversation_events_202608_conversation_id
                          Index Cond: ((conversation_id)::text = (conversations.id)::text)
```

# Experiment `exp7_indexes` — phase `after`

- timestamp: `2026-08-25T07:00:54+00:00`
- git head: `2944874`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp7_indexes.py --phase after`

---

## PostgreSQL plans for the retention DELETEs

### `outbox_events`

indexes: `ix_outbox_events_aggregate_id, ix_outbox_events_published_at, ix_outbox_events_topic, outbox_events_pkey`

```text
Delete on outbox_events  (cost=47.62..724.83 rows=0 width=0)
  ->  Bitmap Heap Scan on outbox_events  (cost=47.62..724.83 rows=3057 width=6)
        Recheck Cond: ((published_at IS NOT NULL) AND (published_at < '2025-08-30 07:00:50.694889+00'::timestamp with time zone))
        ->  Bitmap Index Scan on ix_outbox_events_published_at  (cost=0.00..46.86 rows=3057 width=0)
              Index Cond: ((published_at IS NOT NULL) AND (published_at < '2025-08-30 07:00:50.694889+00'::timestamp with time zone))
```

### `idempotency_keys`

indexes: `idempotency_keys_pkey, idempotency_keys_scope_key_key, ix_idempotency_keys_created_at`

```text
Delete on idempotency_keys  (cost=39.98..868.19 rows=0 width=0)
  ->  Bitmap Heap Scan on idempotency_keys  (cost=39.98..868.19 rows=3057 width=6)
        Recheck Cond: (created_at < '2025-08-30 07:00:50.716011+00'::timestamp with time zone)
        ->  Bitmap Index Scan on ix_idempotency_keys_created_at  (cost=0.00..39.21 rows=3057 width=0)
              Index Cond: (created_at < '2025-08-30 07:00:50.716011+00'::timestamp with time zone)
```

### `conversation_checkpoints`

indexes: `conversation_checkpoints_pkey, ix_conversation_checkpoints_conversation_id, ix_conversation_checkpoints_created_at`

```text
Delete on conversation_checkpoints  (cost=50.85..1006.70 rows=0 width=0)
  ->  Bitmap Heap Scan on conversation_checkpoints  (cost=50.85..1006.70 rows=1529 width=6)
        Recheck Cond: (created_at < '2025-08-30 07:00:50.728812+00'::timestamp with time zone)
        Filter: (NOT (hashed SubPlan 1))
        ->  Bitmap Index Scan on ix_conversation_checkpoints_created_at  (cost=0.00..39.21 rows=3057 width=0)
              Index Cond: (created_at < '2025-08-30 07:00:50.728812+00'::timestamp with time zone)
        SubPlan 1
          ->  Seq Scan on conversations  (cost=0.00..11.00 rows=100 width=90)
                Filter: (checkpoint_id IS NOT NULL)
```

### `runtime_operations`

indexes: `ix_runtime_operations_created_at, runtime_operations_pkey`

```text
Delete on runtime_operations  (cost=39.98..603.83 rows=0 width=0)
  ->  Bitmap Heap Scan on runtime_operations  (cost=39.98..603.83 rows=3057 width=6)
        Recheck Cond: (created_at < '2025-08-30 07:00:50.741815+00'::timestamp with time zone)
        Filter: ((status)::text = ANY ('{SUCCEEDED,FAILED}'::text[]))
        ->  Bitmap Index Scan on ix_runtime_operations_created_at  (cost=0.00..39.21 rows=3057 width=0)
              Index Cond: (created_at < '2025-08-30 07:00:50.741815+00'::timestamp with time zone)
```

### Alembic migration verification (`alembic upgrade head` on a fresh DB)

| table | indexes after migration |
| --- | --- |
| outbox_events | ix_outbox_events_aggregate_id, ix_outbox_events_published_at, ix_outbox_events_topic, outbox_events_pkey |
| idempotency_keys | idempotency_keys_pkey, idempotency_keys_scope_key_key, ix_idempotency_keys_created_at |
| conversation_checkpoints | conversation_checkpoints_pkey, ix_conversation_checkpoints_conversation_id, ix_conversation_checkpoints_created_at |
| runtime_operations | ix_runtime_operations_created_at, runtime_operations_pkey |

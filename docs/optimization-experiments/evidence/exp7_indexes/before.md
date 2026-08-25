# Experiment `exp7_indexes` — phase `before`

- timestamp: `2026-08-25T05:26:27+00:00`
- git head: `307c773`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp7_indexes.py --phase before`

---

## PostgreSQL plans for the retention DELETEs

### `outbox_events`

indexes: `ix_outbox_events_aggregate_id, ix_outbox_events_topic, outbox_events_pkey`

```text
Delete on outbox_events  (cost=0.00..1014.00 rows=0 width=0)
  ->  Seq Scan on outbox_events  (cost=0.00..1014.00 rows=3002 width=6)
        Filter: ((published_at IS NOT NULL) AND (published_at < '2025-08-30 05:26:26.960818+00'::timestamp with time zone))
```

### `idempotency_keys`

indexes: `idempotency_keys_pkey, idempotency_keys_scope_key_key`

```text
Delete on idempotency_keys  (cost=0.00..1165.00 rows=0 width=0)
  ->  Seq Scan on idempotency_keys  (cost=0.00..1165.00 rows=3002 width=6)
        Filter: (created_at < '2025-08-30 05:26:26.977857+00'::timestamp with time zone)
```

### `conversation_checkpoints`

indexes: `conversation_checkpoints_pkey, ix_conversation_checkpoints_conversation_id`

```text
Delete on conversation_checkpoints  (cost=11.25..1371.25 rows=0 width=0)
  ->  Seq Scan on conversation_checkpoints  (cost=11.25..1371.25 rows=1501 width=6)
        Filter: ((created_at < '2025-08-30 05:26:26.9923+00'::timestamp with time zone) AND (NOT (hashed SubPlan 1)))
        SubPlan 1
          ->  Seq Scan on conversations  (cost=0.00..11.00 rows=100 width=90)
                Filter: (checkpoint_id IS NOT NULL)
```

### `runtime_operations`

indexes: `runtime_operations_pkey`

```text
Delete on runtime_operations  (cost=0.00..968.00 rows=0 width=0)
  ->  Seq Scan on runtime_operations  (cost=0.00..968.00 rows=3002 width=6)
        Filter: (((status)::text = ANY ('{SUCCEEDED,FAILED}'::text[])) AND (created_at < '2025-08-30 05:26:27.006898+00'::timestamp with time zone))
```

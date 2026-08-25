# Experiment `exp1_nplus1` — phase `before`

- timestamp: `2026-08-25T05:03:56+00:00`
- git head: `307c773`
- python: `3.12.6`
- database: `postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp`
- command: `python devtools/experiments/exp1_nplus1.py --phase before`

---

## Query counts (statement counter, SELECTs only)
| operation | rows returned | SQL statements |
| --- | --- | --- |
| repository.list_sessions() | 40 | 41 |
| repository.list_conversations() | 30 | 1 |
| repository.list_conversations(session_id=A) | 15 | 1 |
| service.session_events(A, after_seq=0) | 15 | 63 |
| service.stream_session_events(B) one poll cycle | 0 event(s), cycle bounded | 63 |

### sample queries — list_sessions
```
SELECT sessions.id, sessions.workspace_id, sessions.tenant_id, sessions.user_id, sessions.project_id, sessions.metadata,
SELECT container_leases.session_id, container_leases.container_id, container_leases.lease_epoch, container_leases.status
SELECT container_leases.session_id, container_leases.container_id, container_leases.lease_epoch, container_leases.status
SELECT container_leases.session_id, container_leases.container_id, container_leases.lease_epoch, container_leases.status
```

### sample queries — list_conversations
```
SELECT sessions.id, sessions.workspace_id, sessions.tenant_id, sessions.user_id, sessions.project_id, sessions.metadata,
SELECT container_leases.session_id, container_leases.container_id, container_leases.lease_epoch, container_leases.status
SELECT container_leases.session_id, container_leases.container_id, container_leases.lease_epoch, container_leases.status
SELECT container_leases.session_id, container_leases.container_id, container_leases.lease_epoch, container_leases.status
```

### sample queries — session_events
```
SELECT sessions.id, sessions.workspace_id, sessions.tenant_id, sessions.user_id, sessions.project_id, sessions.metadata,
SELECT container_leases.session_id, container_leases.container_id, container_leases.lease_epoch, container_leases.status
SELECT container_leases.session_id, container_leases.container_id, container_leases.lease_epoch, container_leases.status
SELECT container_leases.session_id, container_leases.container_id, container_leases.lease_epoch, container_leases.status
SELECT container_leases.session_id, container_leases.container_id, container_leases.lease_epoch, container_leases.status
SELECT container_leases.session_id, container_leases.container_id, container_leases.lease_epoch, container_leases.status
```

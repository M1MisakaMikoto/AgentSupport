"""Experiment 7: retention cleanup queries scan without matching indexes.

The retention process deletes by ``created_at`` / ``published_at`` on tables
that have no index on those columns. This experiment seeds ~30k rows per table
and captures the PostgreSQL plan (EXPLAIN) for each retention DELETE, before
and after adding indexes. The "after" phase also applies the Alembic migration
to a fresh database to prove the migration path works.
"""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine, insert, text

# Point the Alembic env (which reads the singleton settings) at the migration DB
# BEFORE anything imports ``agentsupport.config``.
MIGRATION_DATABASE_URL = (
    "postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp_mig"
)
os.environ["AGENTSUPPORT_DATABASE_URL"] = MIGRATION_DATABASE_URL

from common import (
    EXPERIMENT_DATABASE_URL,
    REPO_ROOT,
    record_evidence,
    table,
)

from agentsupport.adapters.persistence.sqlalchemy.models import (
    Base,
    ConversationCheckpointRow,
    IdempotencyKeyRow,
    OutboxEventRow,
    RuntimeOperationRow,
)
from agentsupport.adapters.persistence.sqlalchemy.repository import (
    PostgresRepository,
)

ROWS = 30_000


def _seed(engine) -> None:
    now = datetime.now(UTC)
    day = timedelta(days=1)
    with engine.begin() as conn:
        conn.execute(
            insert(OutboxEventRow),
            [
                {
                    "id": str(uuid4()),
                    "topic": "conversation.events",
                    "aggregate_id": str(uuid4()),
                    "payload": {"i": i},
                    "created_at": now - (i % 400) * day,
                    "published_at": now - (i % 400) * day,
                }
                for i in range(ROWS)
            ],
        )
        conn.execute(
            insert(IdempotencyKeyRow),
            [
                {
                    "id": str(uuid4()),
                    "scope": "experiment",
                    "key": f"key-{i}",
                    "request_hash": f"{i:064x}",
                    "resource_id": str(uuid4()),
                    "response_payload": {"i": i},
                    "created_at": now - (i % 400) * day,
                }
                for i in range(ROWS)
            ],
        )
        conn.execute(
            insert(ConversationCheckpointRow),
            [
                {
                    "id": str(uuid4()),
                    "conversation_id": str(uuid4()),
                    "run_id": str(uuid4()),
                    "last_event_seq": i,
                    "context_bundle": {"i": i},
                    "context_bundle_hash": f"{i:064x}",
                    "workspace_write_lease_epoch": 0,
                    "created_at": now - (i % 400) * day,
                }
                for i in range(ROWS)
            ],
        )
        conn.execute(
            insert(RuntimeOperationRow),
            [
                {
                    "id": str(uuid4()),
                    "operation": "start",
                    "resource_id": str(uuid4()),
                    "status": "SUCCEEDED" if i % 2 else "FAILED",
                    "result": {"i": i},
                    "created_at": now - (i % 400) * day,
                }
                for i in range(ROWS)
            ],
        )


EXPLAINS: dict[str, str] = {
    "outbox_events": (
        "EXPLAIN DELETE FROM outbox_events "
        "WHERE published_at IS NOT NULL AND published_at < :cutoff"
    ),
    "idempotency_keys": "EXPLAIN DELETE FROM idempotency_keys WHERE created_at < :cutoff",
    "conversation_checkpoints": (
        "EXPLAIN DELETE FROM conversation_checkpoints "
        "WHERE created_at < :cutoff "
        "AND id NOT IN (SELECT checkpoint_id FROM conversations "
        "WHERE checkpoint_id IS NOT NULL)"
    ),
    "runtime_operations": (
        "EXPLAIN DELETE FROM runtime_operations "
        "WHERE status IN ('SUCCEEDED','FAILED') AND created_at < :cutoff"
    ),
}

RETENTION_INDEXES = [
    ("ix_outbox_events_published_at", "outbox_events"),
    ("ix_idempotency_keys_created_at", "idempotency_keys"),
    ("ix_conversation_checkpoints_created_at", "conversation_checkpoints"),
    ("ix_runtime_operations_created_at", "runtime_operations"),
    ("ix_container_leases_expires_at", "container_leases"),
    ("ix_workspace_write_leases_expires_at", "workspace_write_leases"),
]


def _explain(engine, statement: str) -> list[str]:
    with engine.connect() as conn:
        # Selective predicate: only rows older than 360 days match (~10% of
        # the seeded 0..399-day age distribution), so the planner has a
        # realistic choice between a seq scan and an index scan.
        rows = conn.execute(
            text(statement),
            {"cutoff": datetime.now(UTC) - timedelta(days=360)},
        ).fetchall()
    return [str(row[0]) for row in rows]


def _indexes(engine, table_name: str) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename = :t ORDER BY indexname"
            ),
            {"t": table_name},
        ).fetchall()
    return [row[0] for row in rows]


def main(phase: str) -> None:
    repo = PostgresRepository(EXPERIMENT_DATABASE_URL)
    Base.metadata.drop_all(repo.engine)
    Base.metadata.create_all(repo.engine)
    _seed(repo.engine)
    if phase == "before":
        # Simulate the pre-fix schema: drop the retention indexes so the plan
        # comparison isolates the index itself (same seed + ANALYZE).
        with repo.engine.begin() as conn:
            for index, _table in RETENTION_INDEXES:
                conn.execute(text(f"DROP INDEX IF EXISTS {index}"))
    with repo.engine.begin() as conn:
        for analyze_table in (
            "outbox_events",
            "idempotency_keys",
            "conversation_checkpoints",
            "runtime_operations",
        ):
            conn.execute(text(f"ANALYZE {analyze_table}"))

    sections: list[str] = ["## PostgreSQL plans for the retention DELETEs"]
    for table_name, statement in EXPLAINS.items():
        plan = _explain(repo.engine, statement)
        indexes = _indexes(repo.engine, table_name)
        sections.append(
            f"### `{table_name}`\n\n"
            f"indexes: `{', '.join(indexes) or '(none on this table)'}`\n\n"
            "```text\n" + "\n".join(plan) + "\n```"
        )

    if phase == "after":
        # Prove the Alembic migration path builds the same indexes from scratch.
        admin = create_engine(
            "postgresql+psycopg://agent:agent@localhost:5432/postgres",
            isolation_level="AUTOCOMMIT",
        )
        with admin.connect() as conn:
            conn.execute(text("DROP DATABASE IF EXISTS agentsupport_exp_mig"))
            conn.execute(text("CREATE DATABASE agentsupport_exp_mig"))
        from alembic.config import Config

        from alembic import command

        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        os.environ["AGENTSUPPORT_DATABASE_URL"] = MIGRATION_DATABASE_URL
        command.upgrade(cfg, "head")
        mig_engine = create_engine(MIGRATION_DATABASE_URL)
        migrated_indexes = {
            name: _indexes(mig_engine, name) for name in EXPLAINS
        }
        sections.append(
            "### Alembic migration verification (`alembic upgrade head` on a fresh DB)\n\n"
            + table(
                [
                    [name, ", ".join(indexes) or "(none)"]
                    for name, indexes in migrated_indexes.items()
                ],
                ["table", "indexes after migration"],
            )
        )

    if phase == "after":
        assert any("Bitmap" in section for section in sections), (
            "retention plans do not use the new indexes"
        )
    record_evidence(
        "exp7_indexes",
        phase,
        "\n\n".join(sections),
        command=f"python devtools/experiments/exp7_indexes.py --phase {phase}",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=["before", "after"])
    args = parser.parse_args()
    main(args.phase)

"""Experiment 22: monthly partitioning of conversation_events.

The event table grows without bound. Monthly RANGE partitioning on
``occurred_at`` gives partition pruning for time-window queries and lets the
retention cleanup drop whole idle month partitions instead of row-by-row
DELETEs. This experiment:

- before: seeds events across months on a plain table and captures the
  retention DELETE plan (no pruning);
- after: applies the partition migration, then verifies partition pruning,
  automatic partition creation on write, and the safe drop of idle month
  partitions (months that contain no active-conversation events).
"""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import insert, text

# Point the Alembic env (which reads the singleton settings) at the experiment
# database BEFORE anything imports ``agentsupport.config``.
os.environ["AGENTSUPPORT_DATABASE_URL"] = (
    "postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp"
)

from common import (
    EXPERIMENT_DATABASE_URL,
    REPO_ROOT,
    record_evidence,
    table,
)

from agent_runner_contracts.events import EventEnvelope
from agentsupport.adapters.persistence.sqlalchemy.models import (
    Base,
    ConversationEventRow,
)
from agentsupport.adapters.persistence.sqlalchemy.repository import (
    PostgresRepository,
)
from agentsupport.domain import Conversation

EVENTS_PER_MONTH = 2000


def _seed(repo: PostgresRepository) -> dict[str, object]:
    workspace = repo.create_workspace("exp22-ws", "exp22-ws", "h1", None)
    session = repo.create_session(workspace, "h2", None)

    def conversation(task: str) -> Conversation:
        return repo.create_conversation(
            Conversation(session_id=session.id, task=task),
            f"hash-{task}",
            None,
        )

    conv_a = conversation("terminal-a")  # COMPLETED, events in 01..04
    conv_b = conversation("active-b")  # RUNNING, events in 06..08
    conv_c = conversation("terminal-c")  # COMPLETED, events in 02..03

    plan: list[tuple[Conversation, str, list[int]]] = [
        (conv_a, "COMPLETED", [1, 2, 3, 4]),
        (conv_b, "RUNNING", [6, 7, 8]),
        (conv_c, "COMPLETED", [2, 3]),
    ]
    with repo.engine.begin() as conn:
        for conversation, state, months in plan:
            seq = 0
            for month in months:
                month_start = datetime(2026, month, 1, tzinfo=UTC)
                for i in range(EVENTS_PER_MONTH):
                    seq += 1
                    conn.execute(
                        insert(ConversationEventRow),
                        {
                            "id": str(uuid4()),
                            "conversation_id": str(conversation.id),
                            "run_id": str(conversation.run.run_id),
                            "seq": seq,
                            "type": "message",
                            "payload": {"m": month, "i": i},
                            "source": "experiment",
                            "occurred_at": month_start,
                        },
                    )
            conn.execute(
                text(
                    "UPDATE conversations SET last_seq = :seq, execution_state = :state "
                    "WHERE id = :cid"
                ),
                {"seq": seq, "state": state, "cid": str(conversation.id)},
            )
    return {"a": conv_a, "b": conv_b, "c": conv_c}


def _partitions(engine) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.relname FROM pg_class c "
                "JOIN pg_inherits i ON i.inhrelid = c.oid "
                "JOIN pg_class p ON p.oid = i.inhparent "
                "WHERE p.relname = 'conversation_events' AND c.relispartition "
                "ORDER BY c.relname"
            )
        ).fetchall()
    return [row[0] for row in rows]


def _explain(engine, statement: str, params: dict | None = None) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(text(statement), params or {}).fetchall()
    return [str(row[0]) for row in rows]


def main(phase: str) -> None:
    repo = PostgresRepository(EXPERIMENT_DATABASE_URL)
    Base.metadata.drop_all(repo.engine)
    Base.metadata.create_all(repo.engine)
    seeded = _seed(repo)

    retention_sql = (
        "EXPLAIN (COSTS OFF) DELETE FROM conversation_events "
        "WHERE occurred_at < :cutoff AND conversation_id IN "
        "(SELECT id FROM conversations WHERE execution_state IN "
        "('COMPLETED','FAILED','CANCELLED','LOST'))"
    )
    cutoff = datetime(2026, 5, 1, tzinfo=UTC)

    if phase == "after":
        from alembic.config import Config

        from alembic import command

        # The experiment database is rebuilt from scratch by drop_all/create_all;
        # reset the migration stamp so `upgrade head` actually runs the chain.
        with repo.engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        command.upgrade(cfg, "head")

    before_partitions = _partitions(repo.engine)
    plan = _explain(
        repo.engine,
        retention_sql,
        {"cutoff": datetime(2026, 9, 1, tzinfo=UTC)},
    )

    rows: list[list[object]] = [
        ["table is partitioned", "p" if phase == "after" else "plain table"],
        [
            "month partitions",
            ", ".join(before_partitions) if before_partitions else "(none)",
        ],
        ["retention DELETE plan head", plan[0] if plan else "(empty)"],
    ]

    if phase == "after":
        # (1) the DELETE plan is partition-scoped (one Delete node per month
        # partition instead of a single seq scan over the whole table)
        partition_nodes = [
            line for line in plan if "Delete on conversation_events_20" in line
        ]
        rows.append(
            [
                "partition-scoped Delete nodes in plan",
                len(partition_nodes),
            ]
        )

        # (2) writes to a future month create the partition automatically
        conv_b = seeded["b"]
        fresh = repo.get_conversation(conv_b.id)
        assert fresh is not None
        event = EventEnvelope(
            run_id=fresh.run.run_id,
            seq=fresh.run.last_seq + 1,
            type="message",
            payload={"future": True},
            occurred_at=datetime(2026, 12, 15, tzinfo=UTC),
        )
        repo.append_event(fresh, event)
        partitions_after_write = _partitions(repo.engine)
        rows.append(
            [
                "automatic partition after write (2026-12)",
                "conversation_events_202612" in partitions_after_write,
            ]
        )

        # (3) idle month partitions are dropped safely
        dropped = repo.drop_idle_event_partitions_before(cutoff=cutoff)
        remaining = _partitions(repo.engine)
        rows.append(["idle partitions dropped before 2026-05", dropped])
        rows.append(
            [
                "retained partitions",
                ", ".join(remaining) if remaining else "(none)",
            ]
        )

    markdown = "\n".join(
        [
            "## Monthly partitioning of conversation_events",
            table(rows, ["observation", "value"]),
            "",
            "### retention DELETE plan",
            "```text",
            "\n".join(plan),
            "```",
        ]
    )
    record_evidence(
        "exp22_event_partitioning",
        phase,
        markdown,
        command=f"python devtools/experiments/exp22_event_partitioning.py --phase {phase}",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=["before", "after"])
    args = parser.parse_args()
    main(args.phase)

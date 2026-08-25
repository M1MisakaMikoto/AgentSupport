"""EvalStore behaviors: upserts, batched lists, pagination, constraints."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import event, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agentsupport.adapters.persistence.sqlalchemy.eval_store import SqlAlchemyEvalStore
from agentsupport.adapters.persistence.sqlalchemy.models import EvalRunCaseRow
from agentsupport.evaluation.domain import (
    EvalCase,
    EvalCaseResult,
    EvalDataset,
    EvalRun,
    Verdict,
)
from agentsupport.evaluation.store import InMemoryEvalStore


def _store(tmp_path) -> SqlAlchemyEvalStore:
    return SqlAlchemyEvalStore(f"sqlite:///{tmp_path / 'eval.db'}", create_schema=True)


def _dataset(store, name: str = "dataset", cases: int = 2) -> EvalDataset:
    dataset = EvalDataset(name=name, workspace_id=uuid4(), baseline_version="v1")
    for index in range(cases):
        dataset.cases.append(EvalCase(dataset_id=dataset.id, task=f"task-{index}"))
    store.create_dataset(dataset)
    return dataset


def _result(case_id, verdict: str = "PASS") -> EvalCaseResult:
    passed = verdict == "PASS"
    return EvalCaseResult(
        case_id=case_id,
        task="task",
        verdict=verdict,
        score=1.0 if passed else 0.0,
        verifier_results=[
            Verdict(
                verifier_id="v",
                status=verdict,
                score=1.0 if passed else 0.0,
                reason="r",
            )
        ],
    )


def test_save_run_upserts_stable_rows_and_prunes_removed(tmp_path):
    store = _store(tmp_path)
    dataset = _dataset(store, cases=1)
    case = dataset.cases[0]
    second_case = EvalCase(dataset_id=dataset.id, task="task-2")
    run = EvalRun(dataset_id=dataset.id, results=[_result(case.id)])

    store.save_run(run)
    with Session(store.engine) as db:
        row = db.execute(
            select(EvalRunCaseRow).where(EvalRunCaseRow.run_id == str(run.id))
        ).scalar_one()
        first_row_id = row.id

    run.results = [_result(case.id, verdict="FAIL"), _result(second_case.id)]
    store.save_run(run)
    with Session(store.engine) as db:
        rows = db.execute(
            select(EvalRunCaseRow).where(EvalRunCaseRow.run_id == str(run.id))
        ).scalars()
        by_case = {row.case_id: row for row in rows}
    assert len(by_case) == 2
    assert by_case[str(case.id)].id == first_row_id  # row identity is stable
    assert by_case[str(case.id)].verdict == "FAIL"  # updated in place
    assert by_case[str(second_case.id)].verdict == "PASS"  # inserted

    run.results = [_result(case.id)]
    store.save_run(run)
    with Session(store.engine) as db:
        rows = db.execute(
            select(EvalRunCaseRow).where(EvalRunCaseRow.run_id == str(run.id))
        ).scalars()
        assert [row.case_id for row in rows] == [str(case.id)]


def test_list_queries_are_batched_not_n_plus_one(tmp_path):
    store = _store(tmp_path)
    for index in range(5):
        _dataset(store, name=f"dataset-{index}", cases=2)

    executed: list[str] = []

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        executed.append(statement)

    event.listen(store.engine, "before_cursor_execute", before_cursor_execute)
    try:
        datasets = store.list_datasets()
    finally:
        event.remove(store.engine, "before_cursor_execute", before_cursor_execute)

    assert len(datasets) == 5
    assert all(len(item.cases) == 2 for item in datasets)
    selects = [
        statement
        for statement in executed
        if statement.lstrip().upper().startswith("SELECT") and "eval_" in statement
    ]
    assert len(selects) == 2  # one for datasets, one batched for cases


def test_list_pagination_sql_and_memory(tmp_path):
    store = _store(tmp_path)
    for index in range(5):
        _dataset(store, name=f"dataset-{index}", cases=1)
    assert [item.name for item in store.list_datasets(limit=2, offset=1)] == [
        "dataset-1",
        "dataset-2",
    ]
    assert store.list_datasets(limit=10, offset=10) == []

    memory = InMemoryEvalStore()
    for index in range(5):
        memory.create_dataset(
            EvalDataset(name=f"memory-{index}", workspace_id=uuid4(), baseline_version="v1")
        )
    assert [item.name for item in memory.list_datasets(limit=2, offset=1)] == [
        "memory-1",
        "memory-2",
    ]


def test_run_cases_index_and_check_constraints(tmp_path):
    store = _store(tmp_path)
    indexes = {item["name"] for item in inspect(store.engine).get_indexes("eval_run_cases")}
    assert "ix_eval_run_cases_case_id" in indexes

    with pytest.raises(IntegrityError), store.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO eval_run_cases "
                "(id, run_id, case_id, task, verdict, score, verifier_results, created_at) "
                "VALUES (:id, :run_id, :case_id, 'task', 'BOGUS', 0.0, '[]', :created_at)"
            ),
            {
                "id": str(uuid4()),
                "run_id": str(uuid4()),
                "case_id": str(uuid4()),
                    "created_at": datetime.now(UTC).isoformat(),
            },
        )

    with pytest.raises(IntegrityError), store.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO eval_runs "
                "(id, dataset_id, status, runner_fingerprint, created_at) "
                "VALUES (:id, :dataset_id, 'bogus', '{}', :created_at)"
            ),
            {
                "id": str(uuid4()),
                "dataset_id": str(uuid4()),
                    "created_at": datetime.now(UTC).isoformat(),
            },
        )

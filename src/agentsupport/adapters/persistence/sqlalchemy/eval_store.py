"""SQLAlchemy persistence for the evaluation layer (ADR-004, phase 1)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker

from ....evaluation.domain import (
    CaseRunOutcome,
    EvalCase,
    EvalCaseResult,
    EvalDataset,
    EvalRun,
    Verdict,
    VerifierConfig,
)
from .models import (
    Base,
    EvalCaseRow,
    EvalDatasetRow,
    EvalRunCaseRow,
    EvalRunRow,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class SqlAlchemyEvalStore:
    """SQLAlchemy-backed EvalStore sharing the platform's model metadata."""

    def __init__(
        self,
        database_url: str,
        *,
        create_schema: bool = False,
        engine: Engine | None = None,
    ) -> None:
        self.engine = engine or create_engine(database_url, pool_pre_ping=True)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)
        if create_schema:
            Base.metadata.create_all(self.engine)

    @contextmanager
    def transaction(self) -> Iterator[DbSession]:
        db = self.session_factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # --------------------------------------------------------------- maps
    @staticmethod
    def _verifier_configs(raw: list[dict[str, Any]] | None) -> list[VerifierConfig]:
        return [VerifierConfig.model_validate(item) for item in (raw or [])]

    def _dataset_from_row(
        self,
        db: DbSession,
        row: EvalDatasetRow,
        case_rows: list[EvalCaseRow] | None = None,
    ) -> EvalDataset:
        if case_rows is None:
            case_rows = list(
                db.execute(
                    select(EvalCaseRow)
                    .where(EvalCaseRow.dataset_id == row.id)
                    .order_by(EvalCaseRow.created_at)
                ).scalars()
            )
        return EvalDataset(
            id=UUID(row.id),
            name=row.name,
            description=row.description,
            workspace_id=UUID(row.workspace_id),
            baseline_version=row.baseline_version,
            labels=dict(row.labels or {}),
            cases=[
                EvalCase(
                    id=UUID(case.id),
                    dataset_id=UUID(case.dataset_id),
                    task=case.task,
                    tags=list(case.tags or []),
                    verifiers=self._verifier_configs(case.verifiers),
                    created_at=_as_utc(case.created_at) or _now(),
                )
                for case in case_rows
            ],
            created_at=_as_utc(row.created_at) or _now(),
        )

    @staticmethod
    def _result_from_row(row: EvalRunCaseRow) -> EvalCaseResult:
        return EvalCaseResult(
            case_id=UUID(row.case_id),
            task=row.task,
            verdict=row.verdict,  # type: ignore[arg-type]
            score=row.score,
            verifier_results=[Verdict.model_validate(item) for item in row.verifier_results],
            outcome=CaseRunOutcome.model_validate(row.outcome) if row.outcome else None,
            usage=dict(row.usage) if row.usage else None,
            cost_estimate=row.cost_estimate,
        )

    def _run_from_row(
        self,
        db: DbSession,
        row: EvalRunRow,
        result_rows: list[EvalRunCaseRow] | None = None,
    ) -> EvalRun:
        if result_rows is None:
            result_rows = list(
                db.execute(
                    select(EvalRunCaseRow)
                    .where(EvalRunCaseRow.run_id == row.id)
                    .order_by(EvalRunCaseRow.created_at)
                ).scalars()
            )
        return EvalRun(
            id=UUID(row.id),
            dataset_id=UUID(row.dataset_id),
            status=row.status,  # type: ignore[arg-type]
            runner_fingerprint=dict(row.runner_fingerprint or {}),
            results=[self._result_from_row(item) for item in result_rows],
            summary=dict(row.summary) if row.summary else {},
            created_at=_as_utc(row.created_at) or _now(),
            completed_at=_as_utc(row.completed_at),
        )

    # ------------------------------------------------------------- store
    def create_dataset(self, dataset: EvalDataset) -> EvalDataset:
        with self.transaction() as db:
            db.add(
                EvalDatasetRow(
                    id=str(dataset.id),
                    name=dataset.name,
                    description=dataset.description,
                    workspace_id=str(dataset.workspace_id),
                    baseline_version=dataset.baseline_version,
                    labels=dict(dataset.labels),
                    created_at=dataset.created_at,
                )
            )
            for case in dataset.cases:
                db.add(self._case_row(case))
        return dataset

    @staticmethod
    def _case_row(case: EvalCase) -> EvalCaseRow:
        return EvalCaseRow(
            id=str(case.id),
            dataset_id=str(case.dataset_id),
            task=case.task,
            tags=list(case.tags),
            verifiers=[item.model_dump(mode="json") for item in case.verifiers],
            created_at=case.created_at,
        )

    def get_dataset(self, dataset_id: UUID) -> EvalDataset | None:
        with self.transaction() as db:
            row = db.get(EvalDatasetRow, str(dataset_id))
            return self._dataset_from_row(db, row) if row else None

    def list_datasets(self, limit: int = 100, offset: int = 0) -> list[EvalDataset]:
        with self.transaction() as db:
            rows = db.execute(
                select(EvalDatasetRow)
                .order_by(EvalDatasetRow.created_at)
                .limit(limit)
                .offset(offset)
            ).scalars()
            rows = list(rows)
            if not rows:
                return []
            case_rows = db.execute(
                select(EvalCaseRow)
                .where(EvalCaseRow.dataset_id.in_([row.id for row in rows]))
                .order_by(EvalCaseRow.created_at)
            ).scalars()
            by_dataset: dict[str, list[EvalCaseRow]] = defaultdict(list)
            for case in case_rows:
                by_dataset[case.dataset_id].append(case)
            return [
                self._dataset_from_row(db, row, by_dataset.get(row.id, [])) for row in rows
            ]

    def add_case(self, case: EvalCase) -> EvalCase:
        with self.transaction() as db:
            db.add(self._case_row(case))
        return case

    def save_run(self, run: EvalRun) -> None:
        with self.transaction() as db:
            row = db.get(EvalRunRow, str(run.id))
            if row is None:
                db.add(
                    EvalRunRow(
                        id=str(run.id),
                        dataset_id=str(run.dataset_id),
                        status=run.status,
                        runner_fingerprint=dict(run.runner_fingerprint),
                        summary=run.summary or {},
                        created_at=run.created_at,
                        completed_at=run.completed_at,
                    )
                )
            else:
                row.status = run.status
                row.summary = run.summary or {}
                row.completed_at = run.completed_at
            existing = {
                item.case_id: item
                for item in db.execute(
                    select(EvalRunCaseRow).where(EvalRunCaseRow.run_id == str(run.id))
                ).scalars()
            }
            seen: set[str] = set()
            for result in run.results:
                case_id = str(result.case_id)
                seen.add(case_id)
                old = existing.get(case_id)
                if old is not None:
                    old.task = result.task
                    old.verdict = result.verdict
                    old.score = result.score
                    old.verifier_results = [
                        item.model_dump(mode="json") for item in result.verifier_results
                    ]
                    old.outcome = (
                        result.outcome.model_dump(mode="json") if result.outcome else None
                    )
                    old.usage = result.usage
                    old.cost_estimate = result.cost_estimate
                else:
                    db.add(
                        EvalRunCaseRow(
                            id=str(uuid4()),
                            run_id=str(run.id),
                            case_id=case_id,
                            task=result.task,
                            verdict=result.verdict,
                            score=result.score,
                            verifier_results=[
                                item.model_dump(mode="json")
                                for item in result.verifier_results
                            ],
                            outcome=result.outcome.model_dump(mode="json")
                            if result.outcome
                            else None,
                            usage=result.usage,
                            cost_estimate=result.cost_estimate,
                            created_at=_now(),
                        )
                    )
            for case_id, old in existing.items():
                if case_id not in seen:
                    db.delete(old)

    def get_run(self, run_id: UUID) -> EvalRun | None:
        with self.transaction() as db:
            row = db.get(EvalRunRow, str(run_id))
            return self._run_from_row(db, row) if row else None

    def list_runs(self, limit: int = 100, offset: int = 0) -> list[EvalRun]:
        with self.transaction() as db:
            rows = db.execute(
                select(EvalRunRow).order_by(EvalRunRow.created_at).limit(limit).offset(offset)
            ).scalars()
            rows = list(rows)
            if not rows:
                return []
            result_rows = db.execute(
                select(EvalRunCaseRow)
                .where(EvalRunCaseRow.run_id.in_([row.id for row in rows]))
                .order_by(EvalRunCaseRow.created_at)
            ).scalars()
            by_run: dict[str, list[EvalRunCaseRow]] = defaultdict(list)
            for item in result_rows:
                by_run[item.run_id].append(item)
            return [self._run_from_row(db, row, by_run.get(row.id, [])) for row in rows]

"""Evaluation persistence port and in-memory implementation."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from .domain import EvalCase, EvalDataset, EvalRun


class EvalStore(Protocol):
    def create_dataset(self, dataset: EvalDataset) -> EvalDataset: ...

    def get_dataset(self, dataset_id: UUID) -> EvalDataset | None: ...

    def list_datasets(self, limit: int = 100, offset: int = 0) -> list[EvalDataset]: ...

    def add_case(self, case: EvalCase) -> EvalCase: ...

    def save_run(self, run: EvalRun) -> None: ...

    def get_run(self, run_id: UUID) -> EvalRun | None: ...

    def list_runs(self, limit: int = 100, offset: int = 0) -> list[EvalRun]: ...


class InMemoryEvalStore:
    """Process-local eval persistence (tests and memory mode)."""

    def __init__(self) -> None:
        self._datasets: dict[UUID, EvalDataset] = {}
        self._runs: dict[UUID, EvalRun] = {}

    def create_dataset(self, dataset: EvalDataset) -> EvalDataset:
        self._datasets[dataset.id] = dataset
        return dataset

    def get_dataset(self, dataset_id: UUID) -> EvalDataset | None:
        return self._datasets.get(dataset_id)

    def list_datasets(self, limit: int = 100, offset: int = 0) -> list[EvalDataset]:
        items = sorted(self._datasets.values(), key=lambda item: item.created_at)
        return items[offset : offset + limit]

    def add_case(self, case: EvalCase) -> EvalCase:
        dataset = self._datasets[case.dataset_id]
        dataset.cases.append(case)
        return case

    def save_run(self, run: EvalRun) -> None:
        self._runs[run.id] = run

    def get_run(self, run_id: UUID) -> EvalRun | None:
        return self._runs.get(run_id)

    def list_runs(self, limit: int = 100, offset: int = 0) -> list[EvalRun]:
        items = sorted(self._runs.values(), key=lambda item: item.created_at)
        return items[offset : offset + limit]

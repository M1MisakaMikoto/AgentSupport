"""Phase-1 eval orchestration over the platform execution path.

The eval layer never drives execution itself: each case reuses the platform
execution path (Session/Conversation on the injected AgentSupportService) and
then runs cross-runner verifiers over canonical evidence.  Persistence goes
through the EvalStore port (SQLAlchemy in PostgreSQL mode, in-memory in
memory mode and tests).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from ..application.ports import RepositoryConflict
from ..application.service import AgentSupportService, ServiceError
from ..domain import TERMINAL_STATES, Conversation
from .domain import (
    CaseRunOutcome,
    EvalCase,
    EvalCaseResult,
    EvalDataset,
    EvalReport,
    EvalRun,
    Verdict,
    VerifierConfig,
)
from .store import EvalStore, InMemoryEvalStore
from .verifiers import synthesize, verify_case


def _request_digest(payload: dict[str, Any]) -> str:
    """Stable request fingerprint used by idempotency records."""

    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


class WorkspaceDriver(Protocol):
    def create_from_version(
        self, workspace_id: UUID, version_id: str
    ) -> tuple[UUID, str]: ...

    def list_versions(self, workspace_id: UUID) -> list[dict[str, Any]]: ...

    def delete_workspace(self, workspace_id: UUID) -> None: ...


class EvalService:
    """Eval orchestration over the platform execution path."""

    def __init__(
        self,
        *,
        workspace_driver: WorkspaceDriver,
        agentsupport: AgentSupportService,
        store: EvalStore | None = None,
        case_timeout_seconds: float | None = None,
        case_concurrency: int = 4,
    ) -> None:
        self.workspace_driver = workspace_driver
        self.agentsupport = agentsupport
        self.store = store or InMemoryEvalStore()
        self.case_timeout_seconds = case_timeout_seconds
        self.case_concurrency = case_concurrency
        self._idempotency: dict[tuple[str, str], tuple[str, UUID]] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._recover_stale_runs()

    def _recover_stale_runs(self) -> int:
        """Mark runs left ``running`` by a previous process as failed.

        Background executions (``start_run``) die with the process; without
        this recovery they would stay ``running`` forever in the store.
        Callers can re-run with a fresh idempotency key. This assumes a
        single eval process owns the store; multi-instance deployments need
        run heartbeats/ownership before recovery can be enabled.
        """

        recovered = 0
        for run in self.store.list_runs(limit=10000, offset=0):
            if run.status != "running":
                continue
            run.status = "failed"
            run.completed_at = run.completed_at or datetime.now(UTC)
            run.summary["error"] = {
                "code": "EVAL_PROCESS_RESTARTED",
                "message": "eval run was interrupted by a process restart",
            }
            self.store.save_run(run)
            recovered += 1
        return recovered

    # ------------------------------------------------------------- datasets
    def _idempotent(
        self,
        scope: str,
        key: str | None,
        payload: dict[str, Any],
        create: Any,
        persist: Any,
    ) -> tuple[bool, Any]:
        """Idempotency honoring the platform's Key semantics.

        PostgreSQL mode routes through the repository's idempotency table so
        records survive restarts; the in-memory map is the fallback for memory
        mode. ``persist`` runs before the record is remembered so a replay can
        always resolve the stored resource.
        """

        if not key:
            return True, create()
        digest = _request_digest(payload)
        repository = getattr(self.agentsupport, "repository", None)
        if repository is not None:
            try:
                resource_id = repository.find_idempotent(scope, key, digest)
            except RepositoryConflict as exc:
                raise ServiceError("IDEMPOTENCY_CONFLICT", str(exc), 409) from exc
            if resource_id is not None:
                return False, self._load_idempotent(scope, resource_id)
            entity = create()
            persist(entity)
            try:
                repository.remember_idempotent(
                    scope, key, digest, entity.id, entity.model_dump(mode="json")
                )
            except RepositoryConflict as exc:
                raise ServiceError("IDEMPOTENCY_CONFLICT", str(exc), 409) from exc
            return True, entity
        record = self._idempotency.get((scope, key))
        if record is not None:
            if record[0] != digest:
                raise ServiceError(
                    "IDEMPOTENCY_CONFLICT",
                    "idempotency key was reused with a different request",
                    409,
                )
            return False, self._load_idempotent(scope, record[1])
        entity = create()
        persist(entity)
        self._idempotency[(scope, key)] = (digest, entity.id)
        return True, entity

    def _load_idempotent(self, scope: str, resource_id: UUID) -> Any:
        if scope == "eval_dataset":
            dataset = self.store.get_dataset(resource_id)
            if dataset is None:
                raise ServiceError(
                    "IDEMPOTENCY_CONFLICT", "stored eval dataset no longer exists", 409
                )
            return dataset
        if scope == "eval_run":
            run = self.store.get_run(resource_id)
            if run is None:
                raise ServiceError(
                    "IDEMPOTENCY_CONFLICT", "stored eval run no longer exists", 409
                )
            return run
        raise ServiceError("IDEMPOTENCY_CONFLICT", "unknown idempotent scope", 409)

    def create_dataset(
        self,
        *,
        name: str,
        workspace_id: UUID,
        baseline_version: str,
        description: str = "",
        labels: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> EvalDataset:
        if not any(
            item["version_id"] == baseline_version
            for item in self.workspace_driver.list_versions(workspace_id)
        ):
            raise ServiceError(
                "WORKSPACE_VERSION_NOT_FOUND",
                "baseline version does not exist for the workspace",
                404,
            )
        payload = {
            "name": name,
            "workspace_id": str(workspace_id),
            "baseline_version": baseline_version,
        }
        created, dataset = self._idempotent(
            "eval_dataset",
            idempotency_key,
            payload,
            lambda: EvalDataset(
                name=name,
                description=description,
                workspace_id=workspace_id,
                baseline_version=baseline_version,
                labels=dict(labels or {}),
            ),
            lambda item: self.store.create_dataset(item),
        )
        if not created:
            return dataset
        if idempotency_key is None:
            self.store.create_dataset(dataset)
        return dataset

    def list_datasets(self, limit: int = 100, offset: int = 0) -> list[EvalDataset]:
        return self.store.list_datasets(limit=limit, offset=offset)

    def get_dataset(self, dataset_id: UUID) -> EvalDataset:
        dataset = self.store.get_dataset(dataset_id)
        if dataset is None:
            raise ServiceError("EVAL_DATASET_NOT_FOUND", "eval dataset does not exist", 404)
        return dataset

    def add_case(
        self,
        dataset_id: UUID,
        *,
        task: str,
        tags: list[str] | None = None,
        verifiers: list[VerifierConfig] | None = None,
    ) -> EvalCase:
        self.get_dataset(dataset_id)
        case = EvalCase(
            dataset_id=dataset_id,
            task=task,
            tags=list(tags or []),
            verifiers=list(verifiers or []),
        )
        return self.store.add_case(case)

    # ----------------------------------------------------------------- runs
    def start_run(
        self,
        dataset_id: UUID,
        *,
        runner_fingerprint: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> EvalRun:
        """Create a run and execute it in the background.

        Returns immediately with the run in ``running`` state; callers poll
        ``get_run`` for the terminal state. Idempotent replays return the
        existing run without scheduling a new execution.
        """

        run = self._create_run(
            dataset_id,
            runner_fingerprint=runner_fingerprint,
            idempotency_key=idempotency_key,
        )
        if run.status != "running":
            return run
        task = asyncio.get_running_loop().create_task(self._execute_run(run.id))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return run

    async def run_dataset(
        self,
        dataset_id: UUID,
        *,
        runner_fingerprint: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> EvalRun:
        """Run every case in a dataset, tolerating per-case execution failures.

        A failed case is recorded as an ``ERROR`` result while the remaining
        cases still run. Only dataset-level failures (for example persistence
        errors) mark the run itself as ``failed``; those still raise so the
        caller can see the failure instead of a silently lost run.
        """

        run = self._create_run(
            dataset_id,
            runner_fingerprint=runner_fingerprint,
            idempotency_key=idempotency_key,
        )
        if run.status != "running":
            return run
        return await self._execute_run(run.id)

    def _create_run(
        self,
        dataset_id: UUID,
        *,
        runner_fingerprint: dict[str, str] | None,
        idempotency_key: str | None,
    ) -> EvalRun:
        """Create and persist a run, or return the idempotent replay."""

        self.get_dataset(dataset_id)
        payload = {"dataset_id": str(dataset_id), "fingerprint": runner_fingerprint or {}}
        created, run = self._idempotent(
            "eval_run",
            idempotency_key,
            payload,
            lambda: EvalRun(
                dataset_id=dataset_id,
                runner_fingerprint=dict(runner_fingerprint or {}),
            ),
            lambda item: self.store.save_run(item),
        )
        if not created:
            return run
        if idempotency_key is None:
            self.store.save_run(run)
        run.status = "running"
        self.store.save_run(run)
        return run

    async def _execute_run(self, run_id: UUID) -> EvalRun:
        """Execute every case of a run with bounded concurrency."""

        run = self.store.get_run(run_id)
        dataset = self.store.get_dataset(run.dataset_id) if run is not None else None
        if run is None or dataset is None:
            raise ServiceError("EVAL_RUN_NOT_FOUND", "eval run does not exist", 404)
        semaphore = asyncio.Semaphore(self.case_concurrency)

        async def run_case(case: EvalCase) -> EvalCaseResult:
            async with semaphore:
                try:
                    return await self._run_case(case, dataset)
                except Exception as exc:  # noqa: BLE001 - per-case failures become ERROR results
                    return self._error_result(case, exc)

        try:
            results = await asyncio.gather(*(run_case(case) for case in dataset.cases))
            run.results = list(results)
            run.status = "completed"
        except Exception as exc:  # noqa: BLE001 - dataset-level failures are recorded on the run
            run.status = "failed"
            run.summary["error"] = {"code": type(exc).__name__, "message": str(exc)}
        finally:
            run.completed_at = datetime.now(UTC)
            run.summary = {**self._summarize(run.results), **run.summary}
            try:
                self.store.save_run(run)
            except Exception as exc:
                run.status = "failed"
                run.summary["error"] = {"code": type(exc).__name__, "message": str(exc)}
                raise
        return run

    @staticmethod
    def _error_result(case: EvalCase, exc: Exception) -> EvalCaseResult:
        """Build an ERROR case result from an execution exception."""

        return EvalCaseResult(
            case_id=case.id,
            task=case.task,
            verdict="ERROR",
            score=0.0,
            verifier_results=[
                Verdict(
                    verifier_id="execution",
                    status="ERROR",
                    score=0.0,
                    reason=str(exc),
                    evidence=[type(exc).__name__],
                )
            ],
        )

    async def _run_case(self, case: EvalCase, dataset: EvalDataset) -> EvalCaseResult:
        workspace_id, workspace_path = self.workspace_driver.create_from_version(
            dataset.workspace_id, dataset.baseline_version
        )
        try:
            session = self.agentsupport.create_session(
                workspace_id, None, name=f"eval-{case.id}"
            )
            started = time.perf_counter()
            conversation = await self.agentsupport.create_conversation(session.id, case.task)
            if self.agentsupport.temporal_mode:
                conversation = await self._wait_for_temporal_case(conversation)
            duration = time.perf_counter() - started
            outcome = CaseRunOutcome(
                conversation_id=conversation.id,
                terminal_state=conversation.run.state.value,
                events=[
                    event.model_dump(mode="json")
                    for event in self.agentsupport.events(conversation.id)
                ],
                final_result=conversation.run.result_summary,
                error=conversation.run.error,
                duration_seconds=round(duration, 3),
                workspace_id=workspace_id,
                workspace_path=workspace_path,
            )
            verdicts = await verify_case(outcome, case.verifiers)
            verdict, score = synthesize(verdicts)
            return EvalCaseResult(
                case_id=case.id,
                task=case.task,
                verdict=verdict,
                score=score,
                verifier_results=verdicts,
                outcome=outcome,
                usage=outcome.usage,
            )
        finally:
            self._cleanup_workspace(workspace_id)

    def _cleanup_workspace(self, workspace_id: UUID) -> None:
        """Best-effort removal of the disposable case workspace clone."""

        delete = getattr(self.workspace_driver, "delete_workspace", None)
        if delete is None:
            return
        with suppress(Exception):
            delete(workspace_id)

    async def _wait_for_temporal_case(self, conversation: Conversation) -> Conversation:
        """Await the Temporal workflow's terminal state, then reload the projection.

        ``create_conversation`` returns as soon as the workflow is submitted;
        the case outcome must reflect the completed run, so evaluation waits
        for the workflow (up to ``case_timeout_seconds``) before collecting
        evidence. A timeout cancels the workflow and re-raises so the caller
        records the case as an ERROR.
        """

        temporal = self.agentsupport.temporal
        if temporal is None:
            raise ServiceError(
                "TEMPORAL_UNAVAILABLE", "temporal coordinator is not configured", 503
            )
        try:
            result = await temporal.wait_for_run(
                str(conversation.run.run_id),
                timeout_seconds=self.case_timeout_seconds,
            )
        except TimeoutError:
            with suppress(Exception):
                await temporal.cancel(str(conversation.run.run_id))
            raise
        conversation = self.agentsupport.get_conversation(conversation.id)
        if conversation.run.state not in TERMINAL_STATES:
            raise ServiceError(
                "EVAL_WORKFLOW_FAILED",
                f"workflow finished without a terminal conversation state: {result}",
                502,
            )
        return conversation

    @staticmethod
    def _summarize(results: list[EvalCaseResult]) -> dict[str, Any]:
        counts = {"PASS": 0, "FAIL": 0, "ERROR": 0, "UNCERTAIN": 0}
        for result in results:
            counts[result.verdict] += 1
        considered = len(results) - counts["ERROR"]
        pass_rate = round(counts["PASS"] / considered, 4) if considered else 0.0
        total_usage: dict[str, int] = {}
        for result in results:
            for key, value in (result.usage or {}).items():
                total_usage[key] = total_usage.get(key, 0) + int(value or 0)
        return {
            "total": len(results),
            "passed": counts["PASS"],
            "failed": counts["FAIL"],
            "error": counts["ERROR"],
            "uncertain": counts["UNCERTAIN"],
            "pass_rate": pass_rate,
            "total_duration_seconds": round(
                sum((result.outcome.duration_seconds if result.outcome else 0) for result in results),
                3,
            ),
            "usage": total_usage,
        }

    def get_run(self, run_id: UUID) -> EvalRun:
        run = self.store.get_run(run_id)
        if run is None:
            raise ServiceError("EVAL_RUN_NOT_FOUND", "eval run does not exist", 404)
        return run

    def list_runs(self, limit: int = 100, offset: int = 0) -> list[EvalRun]:
        return self.store.list_runs(limit=limit, offset=offset)

    def report(self, run_id: UUID) -> EvalReport:
        run = self.get_run(run_id)
        return EvalReport(
            run_id=run.id,
            dataset_id=run.dataset_id,
            fingerprint=run.runner_fingerprint,
            summary=run.summary,
            cases=[
                {
                    "case_id": result.case_id,
                    "task": result.task,
                    "verdict": result.verdict,
                    "score": result.score,
                    "reasons": result.reasons,
                    "metrics": {
                        "duration_seconds": result.outcome.duration_seconds
                        if result.outcome
                        else None,
                        "tool_calls": sum(
                            1
                            for event in (result.outcome.events if result.outcome else [])
                            if event.get("type") == "tool.call"
                        ),
                        "interactions": sum(
                            1
                            for event in (result.outcome.events if result.outcome else [])
                            if event.get("type") == "interaction.requested"
                        ),
                        "usage": result.usage,
                    },
                }
                for result in run.results
            ],
        )

    def compare(self, baseline_run_id: UUID, candidate_run_id: UUID) -> dict[str, Any]:
        """Per-case verdict deltas; regression = PASS -> non-PASS."""

        baseline = self.get_run(baseline_run_id)
        candidate = self.get_run(candidate_run_id)
        # Case identity follows the stable case id; task text remains as a
        # fallback for runs recorded before case ids were compared.
        baseline_by_case = {item.case_id: item for item in baseline.results}
        baseline_by_task = {item.task: item for item in baseline.results}
        deltas: list[dict[str, Any]] = []
        regressions = improvements = unchanged = 0
        for item in candidate.results:
            old = baseline_by_case.get(item.case_id) or baseline_by_task.get(item.task)
            old_verdict = old.verdict if old else None
            delta = {
                "case_id": item.case_id,
                "baseline_case_id": old.case_id if old else None,
                "task": item.task,
                "baseline": old_verdict,
                "candidate": item.verdict,
            }
            if old is not None:
                if old.verdict == "PASS" and item.verdict != "PASS":
                    delta["change"] = "regression"
                    regressions += 1
                elif old.verdict != "PASS" and item.verdict == "PASS":
                    delta["change"] = "improvement"
                    improvements += 1
                else:
                    delta["change"] = "unchanged"
                    unchanged += 1
            else:
                delta["change"] = "new"
            deltas.append(delta)
        return {
            "baseline_run_id": baseline_run_id,
            "candidate_run_id": candidate_run_id,
            "same_fingerprint": baseline.runner_fingerprint == candidate.runner_fingerprint,
            "regressions": regressions,
            "improvements": improvements,
            "unchanged": unchanged,
            "deltas": deltas,
        }

"""Phase-1 evaluation domain models (trial slice, in-memory)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class VerifierConfig(BaseModel):
    type: str
    params: dict[str, Any] = Field(default_factory=dict)


class EvalCase(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    dataset_id: UUID
    task: str
    tags: list[str] = Field(default_factory=list)
    verifiers: list[VerifierConfig] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class EvalDataset(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    description: str = ""
    workspace_id: UUID
    baseline_version: str
    labels: dict[str, str] = Field(default_factory=dict)
    cases: list[EvalCase] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class CaseRunOutcome(BaseModel):
    """Canonical execution evidence collected for one case.

    Only platform-owned data: terminal state, the event stream, the final
    result (with normalized usage) and the post-run workspace reference.
    """

    conversation_id: UUID
    terminal_state: str
    events: list[dict[str, Any]] = Field(default_factory=list)
    final_result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    duration_seconds: float = 0.0
    workspace_id: UUID
    workspace_path: str

    @property
    def usage(self) -> dict[str, Any] | None:
        for event in reversed(self.events):
            if event.get("type") == "run.completed":
                result = event.get("payload", {}).get("result") or {}
                usage = result.get("usage")
                if usage is not None:
                    return usage
        return None


VerdictStatus = Literal["PASS", "FAIL", "ERROR", "UNCERTAIN"]


class Verdict(BaseModel):
    verifier_id: str
    status: VerdictStatus
    score: float = Field(ge=0.0, le=1.0)
    reason: str
    evidence: list[str] = Field(default_factory=list)


class EvalCaseResult(BaseModel):
    case_id: UUID
    task: str
    verdict: VerdictStatus
    score: float = Field(ge=0.0, le=1.0)
    verifier_results: list[Verdict] = Field(default_factory=list)
    outcome: CaseRunOutcome | None = None
    usage: dict[str, Any] | None = None
    cost_estimate: float | None = None

    @property
    def reasons(self) -> list[str]:
        return [
            f"{item.verifier_id}: {item.reason}" for item in self.verifier_results
        ]


class EvalRun(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    dataset_id: UUID
    status: Literal["pending", "running", "completed", "failed"] = "pending"
    runner_fingerprint: dict[str, str] = Field(default_factory=dict)
    results: list[EvalCaseResult] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None


class EvalReport(BaseModel):
    run_id: UUID
    dataset_id: UUID
    fingerprint: dict[str, str] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)
    cases: list[dict[str, Any]] = Field(default_factory=list)

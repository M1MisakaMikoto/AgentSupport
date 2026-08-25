"""Evaluation layer (Phase-1 trial slice).

Cross-runner by construction: verifiers consume only canonical data (post-run
workspace state, platform event stream, normalized usage/final result). See
``docs/adr/004-evaluation-layer.md`` and ``docs/evaluation/``.
"""

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
from .service import EvalService
from .verifiers import Verifier, get_verifier, verify_case

__all__ = [
    "CaseRunOutcome",
    "EvalCase",
    "EvalCaseResult",
    "EvalDataset",
    "EvalReport",
    "EvalRun",
    "EvalService",
    "Verdict",
    "Verifier",
    "VerifierConfig",
    "get_verifier",
    "verify_case",
]

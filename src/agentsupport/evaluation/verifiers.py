"""Built-in cross-runner verifiers (Phase-1 trial slice).

Every verifier here reads only canonical data (``CaseRunOutcome`` plus the
post-run workspace path) so it stays valid when the runner changes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from .domain import CaseRunOutcome, Verdict, VerifierConfig

Verifier = Callable[[CaseRunOutcome, dict[str, Any]], Awaitable[Verdict]]

DEFAULT_VERIFIERS = ("terminal_state", "convergence")


def _verdict(verifier_id: str, status: str, score: float, reason: str, *evidence: str) -> Verdict:
    return Verdict(
        verifier_id=verifier_id,
        status=status,  # type: ignore[arg-type]
        score=score,
        reason=reason,
        evidence=list(evidence),
    )


async def terminal_state(outcome: CaseRunOutcome, params: dict[str, Any]) -> Verdict:
    expect = str(params.get("expect", "COMPLETED"))
    if outcome.terminal_state == expect:
        return _verdict(
            "terminal_state",
            "PASS",
            1.0,
            f"run reached expected terminal state {expect}",
            f"terminal_state={outcome.terminal_state}",
        )
    if outcome.terminal_state in {"FAILED", "CANCELLED", "LOST"}:
        return _verdict(
            "terminal_state",
            "FAIL",
            0.0,
            f"run ended in {outcome.terminal_state}, expected {expect}",
            f"terminal_state={outcome.terminal_state}",
            f"error={outcome.error}",
        )
    return _verdict(
        "terminal_state",
        "ERROR",
        0.0,
        f"run is stuck in non-terminal state {outcome.terminal_state}",
        f"terminal_state={outcome.terminal_state}",
    )


async def convergence(outcome: CaseRunOutcome, params: dict[str, Any]) -> Verdict:
    max_error_events = int(params.get("max_error_events", 0))
    failed = [
        event for event in outcome.events if event.get("type") == "run.failed"
    ]
    if len(failed) > max_error_events:
        return _verdict(
            "convergence",
            "FAIL",
            0.0,
            f"{len(failed)} run.failed event(s), allowed {max_error_events}",
            f"failed_events={len(failed)}",
        )
    return _verdict(
        "convergence",
        "PASS",
        1.0,
        f"converged with {len(failed)} error event(s)",
        f"failed_events={len(failed)}",
    )


def _result_text(outcome: CaseRunOutcome) -> list[str]:
    texts: list[str] = []
    result = outcome.final_result or {}
    content = result.get("content")
    if isinstance(content, str) and content:
        texts.append(content)
    for event in outcome.events:
        if event.get("type") == "message":
            content = (event.get("payload") or {}).get("content")
            if isinstance(content, str) and content:
                texts.append(content)
    return texts


async def output_rules(outcome: CaseRunOutcome, params: dict[str, Any]) -> Verdict:
    texts = "\n".join(_result_text(outcome))
    for needle in params.get("contains", []):
        if str(needle) not in texts:
            return _verdict(
                "output_rules",
                "FAIL",
                0.0,
                f"output does not contain: {needle!r}",
            )
    for needle in params.get("not_contains", []):
        if str(needle) in texts:
            return _verdict(
                "output_rules",
                "FAIL",
                0.0,
                f"output unexpectedly contains: {needle!r}",
            )
    return _verdict(
        "output_rules",
        "PASS",
        1.0,
        "output content rules satisfied",
        f"content_len={len(texts)}",
    )


async def test_command(outcome: CaseRunOutcome, params: dict[str, Any]) -> Verdict:
    command = str(params.get("command") or "").strip()
    if not command:
        return _verdict(
            "test_command",
            "ERROR",
            0.0,
            "test_command verifier requires a command parameter",
        )
    timeout = float(params.get("timeout_seconds", 120))
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=outcome.workspace_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        return _verdict(
            "test_command",
            "ERROR",
            0.0,
            f"verification command timed out after {timeout}s",
            f"command={command}",
        )
    if process.returncode == 0:
        return _verdict(
            "test_command",
            "PASS",
            1.0,
            "verification command passed (exit 0)",
            f"command={command}",
        )
    tail = "\n".join(
        (stdout or b"").decode(errors="replace").splitlines()[-8:]
        + (stderr or b"").decode(errors="replace").splitlines()[-8:]
    )
    return _verdict(
        "test_command",
        "FAIL",
        0.0,
        f"verification command failed (exit {process.returncode})",
        f"command={command}",
        f"output_tail={tail!r}",
    )


VERIFIERS: dict[str, Verifier] = {
    "terminal_state": terminal_state,
    "convergence": convergence,
    "output_rules": output_rules,
    "test_command": test_command,
}


def get_verifier(verifier_type: str) -> Verifier:
    try:
        return VERIFIERS[verifier_type]
    except KeyError as exc:
        raise KeyError(f"unknown verifier: {verifier_type}") from exc


async def verify_case(
    outcome: CaseRunOutcome,
    verifiers: list[VerifierConfig],
) -> list[Verdict]:
    """Run a case's configured verifiers; unknown ones become ERROR verdicts."""

    configured = verifiers or [
        VerifierConfig(type=name) for name in DEFAULT_VERIFIERS
    ]
    results: list[Verdict] = []
    for config in configured:
        try:
            verifier = get_verifier(config.type)
        except KeyError:
            results.append(
                _verdict(
                    config.type,
                    "ERROR",
                    0.0,
                    f"unknown verifier: {config.type}",
                )
            )
            continue
        try:
            results.append(await verifier(outcome, config.params))
        except Exception as exc:  # noqa: BLE001 - verifier bugs become ERROR
            results.append(
                _verdict(
                    config.type,
                    "ERROR",
                    0.0,
                    f"verifier failed: {type(exc).__name__}: {exc}",
                )
            )
    return results


def synthesize(verdicts: list[Verdict]) -> tuple[str, float]:
    """Combine per-verifier verdicts into one case verdict + score."""

    if not verdicts:
        return "ERROR", 0.0
    if any(item.status == "ERROR" for item in verdicts):
        return "ERROR", 0.0
    if any(item.status == "FAIL" for item in verdicts):
        return "FAIL", 0.0
    if any(item.status == "UNCERTAIN" for item in verdicts):
        return "UNCERTAIN", sum(item.score for item in verdicts) / len(verdicts)
    return "PASS", sum(item.score for item in verdicts) / len(verdicts)

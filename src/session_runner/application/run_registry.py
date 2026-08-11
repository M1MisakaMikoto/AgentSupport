from __future__ import annotations

import time
from uuid import UUID

from ..domain import RunState

#: Statuses that never transition back to active work. Terminal runs are kept
#: briefly so clients can still read their events, then evicted to bound the
#: memory of a long-lived runner process.
TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "LOST"})


class RunRegistry:
    """Process-local ownership boundary for active Runner state.

    Terminal runs are retained for ``retain_terminal_seconds`` (or until the
    ``max_retained_terminal`` cap is exceeded, evicting oldest first) so late
    event reads still work while the process memory stays bounded.
    """

    def __init__(
        self,
        *,
        retain_terminal_seconds: float = 30 * 60,
        max_retained_terminal: int = 200,
    ) -> None:
        self._runs: dict[UUID, RunState] = {}
        self._terminal_at: dict[UUID, float] = {}
        self.retain_terminal_seconds = retain_terminal_seconds
        self.max_retained_terminal = max_retained_terminal

    def _observe(self, run_id: UUID) -> None:
        state = self._runs.get(run_id)
        if state is not None and state.status in TERMINAL_STATUSES:
            self._terminal_at.setdefault(run_id, time.monotonic())

    def prune(self, now: float | None = None) -> int:
        """Drop terminal runs past the retention window or above the cap.

        Active runs are never pruned. Returns the number of runs removed.
        """

        current = time.monotonic() if now is None else now
        expired = [
            run_id
            for run_id, finished_at in self._terminal_at.items()
            if current - finished_at > self.retain_terminal_seconds
        ]
        for run_id in expired:
            self._runs.pop(run_id, None)
            self._terminal_at.pop(run_id, None)
        terminal = sorted(
            (run_id for run_id in self._terminal_at if run_id in self._runs),
            key=self._terminal_at.__getitem__,
        )
        excess = max(len(terminal) - self.max_retained_terminal, 0)
        for run_id in terminal[:excess]:
            self._runs.pop(run_id, None)
            self._terminal_at.pop(run_id, None)
        return len(expired) + excess

    def get(self, run_id: UUID) -> RunState | None:
        state = self._runs.get(run_id)
        if state is not None:
            self._observe(run_id)
        return state

    def __contains__(self, run_id: UUID) -> bool:
        return run_id in self._runs

    def __getitem__(self, run_id: UUID) -> RunState:
        state = self.get(run_id)
        if state is None:
            raise KeyError(run_id)
        return state

    def __setitem__(self, run_id: UUID, state: RunState) -> None:
        self.prune()
        self._runs[run_id] = state
        self._observe(run_id)

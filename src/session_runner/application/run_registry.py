from uuid import UUID

from ..domain import RunState


class RunRegistry:
    """Process-local ownership boundary for active Runner state."""

    def __init__(self) -> None:
        self._runs: dict[UUID, RunState] = {}

    def get(self, run_id: UUID) -> RunState | None:
        return self._runs.get(run_id)

    def __contains__(self, run_id: UUID) -> bool:
        return run_id in self._runs

    def __getitem__(self, run_id: UUID) -> RunState:
        return self._runs[run_id]

    def __setitem__(self, run_id: UUID, state: RunState) -> None:
        self._runs[run_id] = state

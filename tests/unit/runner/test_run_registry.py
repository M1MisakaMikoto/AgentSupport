from uuid import uuid4

from session_runner.application.run_registry import RunRegistry


class _FakeState:
    def __init__(self, status: str) -> None:
        self.status = status


def test_active_runs_are_never_pruned():
    registry = RunRegistry(retain_terminal_seconds=1, max_retained_terminal=0)
    run_id = uuid4()
    registry[run_id] = _FakeState("RUNNING")

    assert registry.prune(now=1000.0) == 0
    assert run_id in registry
    assert registry.get(run_id) is not None


def test_terminal_run_is_pruned_after_retention_window():
    registry = RunRegistry(retain_terminal_seconds=60, max_retained_terminal=100)
    run_id = uuid4()
    registry[run_id] = _FakeState("COMPLETED")
    registry._terminal_at[run_id] = 1000.0

    assert run_id in registry
    assert registry.prune(now=1000.0 + 61) == 1
    assert run_id not in registry
    assert registry.get(run_id) is None


def test_terminal_time_is_recorded_lazily_on_access():
    registry = RunRegistry(retain_terminal_seconds=60, max_retained_terminal=100)
    run_id = uuid4()
    state = _FakeState("STARTING")
    registry[run_id] = state
    state.status = "CANCELLED"

    assert registry.get(run_id) is state
    assert run_id in registry._terminal_at
    registry.prune(now=registry._terminal_at[run_id] + 61)
    assert run_id not in registry


def test_capacity_evicts_oldest_terminal_runs_first():
    registry = RunRegistry(retain_terminal_seconds=3600, max_retained_terminal=2)
    first, second, third = uuid4(), uuid4(), uuid4()
    for run_id in (first, second, third):
        registry[run_id] = _FakeState("FAILED")
    registry._terminal_at[first] = 100.0
    registry._terminal_at[second] = 200.0
    registry._terminal_at[third] = 300.0

    assert registry.prune(now=1000.0) == 1
    assert first not in registry
    assert second in registry
    assert third in registry


def test_capacity_ignores_active_runs():
    registry = RunRegistry(retain_terminal_seconds=3600, max_retained_terminal=1)
    active, done = uuid4(), uuid4()
    registry[active] = _FakeState("RUNNING")
    registry[done] = _FakeState("COMPLETED")
    registry._terminal_at[done] = 1.0

    assert registry.prune(now=2.0) == 0
    assert active in registry
    assert done in registry


def test_prune_is_idempotent_with_missing_runs():
    registry = RunRegistry(retain_terminal_seconds=1, max_retained_terminal=1)
    stale = uuid4()
    registry._terminal_at[stale] = 100.0
    assert registry.prune(now=200.0) == 1
    assert registry.prune(now=200.0) == 0

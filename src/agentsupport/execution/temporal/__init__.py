"""Temporal execution backend (phase 0 prototype).

Replaces the ``ExecutionJob`` / ``RunCommand`` / checkpoint repository
plumbing with one durable workflow per run.  See the integration plan in
``.dev/lab/temporal-experiment/PLAN.md``.
"""

from .client import TemporalRunCoordinator

__all__ = ["TemporalRunCoordinator"]

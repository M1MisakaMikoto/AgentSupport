"""Process entry point for runner heartbeat reconciliation."""

from .processes.reconciler import RunnerHeartbeatReconciler, main

__all__ = ["RunnerHeartbeatReconciler", "main"]

if __name__ == "__main__":
    main()

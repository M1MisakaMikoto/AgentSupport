"""Process entry point for reconciliation."""

from .processes.reconciler import DistributedReconciler, main

__all__ = ["DistributedReconciler", "main"]

if __name__ == "__main__":
    main()

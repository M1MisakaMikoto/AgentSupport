"""Compatibility process entry point for the distributed worker."""

from .processes.worker import DistributedWorker, OwnedRun, main

__all__ = ["DistributedWorker", "OwnedRun", "main"]

if __name__ == "__main__":
    main()

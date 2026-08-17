"""Process entry point for the Temporal execution worker."""

from .execution.temporal.worker import main

__all__ = ["main"]

if __name__ == "__main__":
    main()

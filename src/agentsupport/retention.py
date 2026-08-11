"""Process entry point for the periodic PostgreSQL retention cleanup."""

from .processes.retention import RetentionCleaner, main

__all__ = ["RetentionCleaner", "main"]

if __name__ == "__main__":
    main()

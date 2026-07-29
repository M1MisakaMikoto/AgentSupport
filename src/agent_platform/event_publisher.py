"""Compatibility process entry point for outbox publication."""

from .processes.outbox_publisher import OutboxPublisher, main

__all__ = ["OutboxPublisher", "main"]

if __name__ == "__main__":
    main()

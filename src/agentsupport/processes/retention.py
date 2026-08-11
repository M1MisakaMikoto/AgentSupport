from __future__ import annotations

import asyncio

from ..adapters.persistence.sqlalchemy.repository import PostgresRepository
from ..bootstrap.container import build_repository
from ..bootstrap.settings import Settings, settings


class RetentionCleaner:
    """Periodically deletes transient rows that outlived their retention windows."""

    def __init__(
        self,
        config: Settings = settings,
        *,
        repository: PostgresRepository | None = None,
    ) -> None:
        if config.persistence_mode != "postgres":
            raise ValueError("retention cleanup requires PostgreSQL persistence mode")
        self.config = config
        self.repository = repository or build_repository(config)
        assert self.repository is not None

    def run_once(self) -> dict[str, int]:
        return self.repository.retention_cleanup(
            idempotency_hours=self.config.retention_idempotency_hours,
            unreferenced_checkpoints_hours=self.config.retention_unreferenced_checkpoints_hours,
            published_outbox_days=self.config.retention_published_outbox_days,
            terminal_jobs_days=self.config.retention_terminal_jobs_days,
            terminal_events_days=self.config.retention_terminal_events_days,
        )

    async def serve_forever(self) -> None:
        while True:
            await asyncio.to_thread(self.run_once)
            await asyncio.sleep(self.config.retention_cleanup_interval_hours * 3600)


def main() -> None:
    asyncio.run(RetentionCleaner().serve_forever())


if __name__ == "__main__":
    main()

from __future__ import annotations

import asyncio
import os
import socket
from uuid import uuid4

from ..adapters.notification.notifier import EventNotifier, create_event_notifier
from ..adapters.persistence.sqlalchemy.repository import PostgresRepository
from ..bootstrap.container import build_repository
from ..bootstrap.settings import Settings, settings
from ..observability import logging as obs_logging


class OutboxPublisher:
    def __init__(
        self,
        config: Settings = settings,
        *,
        repository: PostgresRepository | None = None,
        notifier: EventNotifier | None = None,
    ) -> None:
        if config.persistence_mode != "postgres":
            raise ValueError("outbox publisher requires PostgreSQL persistence mode")
        self.config = config
        self.publisher_id = config.instance_id or (
            f"{socket.gethostname()}:{os.getpid()}:publisher:{uuid4().hex[:8]}"
        )
        self.repository = repository or build_repository(config)
        assert self.repository is not None
        self.notifier = notifier or create_event_notifier(config.redis_url)

    async def run_once(self) -> int:
        notifications = self.repository.claim_outbox(self.publisher_id)
        published = 0
        for notification in notifications:
            try:
                await self.notifier.publish(
                    notification.topic, notification.aggregate_id, notification.payload
                )
            except Exception:  # noqa: BLE001 - the leased outbox item is released for retry
                self.repository.finish_outbox(
                    notification.id, self.publisher_id, published=False
                )
            else:
                self.repository.finish_outbox(
                    notification.id, self.publisher_id, published=True
                )
                published += 1
        return published

    async def serve_forever(self) -> None:
        try:
            while True:
                published = await self.run_once()
                if not published:
                    await asyncio.sleep(self.config.event_poll_interval_seconds)
        finally:
            await self.notifier.close()


def main() -> None:
    obs_logging.configure_logging(
        log_format=settings.log_format,
        level=settings.log_level,
        service_name=settings.service_name,
    )
    asyncio.run(OutboxPublisher().serve_forever())


if __name__ == "__main__":
    main()

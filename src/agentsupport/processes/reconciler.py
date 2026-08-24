"""Runner heartbeat reconciler.

The Temporal execution backend owns run lifecycle, container orchestration,
heartbeats and cleanup.  What remains here is runner self-registration
heartbeat expiry for the shared-token registry.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from ..adapters.persistence.sqlalchemy.repository import PostgresRepository
from ..bootstrap.container import build_repository
from ..bootstrap.settings import Settings, settings
from ..observability import logging as obs_logging
from ..observability import metrics as obs_metrics


class RunnerHeartbeatReconciler:
    """Expire stale runner self-registrations; container lifecycle is Temporal's."""

    def __init__(
        self,
        config: Settings = settings,
        *,
        repository: PostgresRepository | None = None,
    ) -> None:
        if config.persistence_mode != "postgres":
            raise ValueError("reconciler requires PostgreSQL persistence mode")
        self.config = config
        self.repository = repository or build_repository(config)
        assert self.repository is not None

    async def run_once(self) -> dict[str, int]:
        expired_runners = len(
            self.repository.expire_runner_registrations(
                now=datetime.now(UTC),
                timeout_seconds=self.config.runner_heartbeat_timeout_seconds,
            )
        )
        if expired_runners:
            obs_metrics.record_runner_heartbeat_expired()
        return {"expired_runners": expired_runners}

    async def serve_forever(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(self.config.health_check_interval_seconds)


def main() -> None:
    obs_logging.configure_logging(
        log_format=settings.log_format,
        level=settings.log_level,
        service_name=settings.service_name,
    )
    asyncio.run(RunnerHeartbeatReconciler().serve_forever())


if __name__ == "__main__":
    main()

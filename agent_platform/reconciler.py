from __future__ import annotations

import asyncio

from .config import Settings, settings
from .ports import RuntimeDriver
from .repository import PostgresRepository
from .worker import _runtime_driver


class DistributedReconciler:
    """Reconciles durable Runner ownership with the runtime provider's actual resources."""

    def __init__(
        self,
        config: Settings = settings,
        *,
        repository: PostgresRepository | None = None,
        runtime_driver: RuntimeDriver | None = None,
    ) -> None:
        if config.persistence_mode != "postgres":
            raise ValueError("distributed reconciler requires PostgreSQL persistence mode")
        self.config = config
        self.repository = repository or PostgresRepository(
            config.database_url, create_schema=config.auto_create_schema
        )
        self.runtime_driver = runtime_driver or _runtime_driver(config)
        self.runtime_reconciliation_enabled = (
            runtime_driver is not None or config.runtime_driver != "memory"
        )

    async def run_once(self) -> dict[str, int]:
        paused_endpoints = self.repository.pause_expired_waiting(
            self.config.waiting_input_timeout_seconds
        )
        for endpoint in paused_endpoints:
            await self.runtime_driver.stop(endpoint.runtime_id)
        if not self.runtime_reconciliation_enabled:
            return {
                "paused": len(paused_endpoints),
                "missing": 0,
                "removed_orphans": 0,
            }
        missing = 0
        removed_orphans = 0
        endpoints = self.repository.list_active_runner_endpoints()
        active_runtime_ids = {endpoint.runtime_id for endpoint in endpoints}
        for endpoint in endpoints:
            inspection = await self.runtime_driver.inspect(endpoint.runtime_id)
            if inspection.get("status") not in {"running", "created"}:
                self.repository.mark_runtime_missing(endpoint.run_id)
                missing += 1
        list_managed = getattr(self.runtime_driver, "list_managed", None)
        if list_managed is not None:
            for item in await list_managed():
                runtime_id = item.get("metadata", {}).get("name")
                if runtime_id and runtime_id not in active_runtime_ids:
                    await self.runtime_driver.stop(runtime_id, force=True)
                    removed_orphans += 1
        return {
            "paused": len(paused_endpoints),
            "missing": missing,
            "removed_orphans": removed_orphans,
        }

    async def serve_forever(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(self.config.health_check_interval_seconds)


def main() -> None:
    asyncio.run(DistributedReconciler().serve_forever())


if __name__ == "__main__":
    main()

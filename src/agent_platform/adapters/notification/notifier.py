from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any
from uuid import UUID

from ...application.ports import EventNotifier


class InMemoryEventNotifier:
    """Process-local notifier used by tests and as the database-polling fallback."""

    def __init__(self) -> None:
        self._conditions: dict[tuple[str, UUID], asyncio.Condition] = defaultdict(asyncio.Condition)

    async def publish(self, topic: str, aggregate_id: UUID, payload: dict[str, Any]) -> None:
        del payload
        condition = self._conditions[(topic, aggregate_id)]
        async with condition:
            condition.notify_all()

    async def wait(self, topic: str, aggregate_id: UUID, timeout: float) -> bool:
        condition = self._conditions[(topic, aggregate_id)]
        try:
            async with condition:
                await asyncio.wait_for(condition.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    async def close(self) -> None:
        return None


class RedisEventNotifier:
    def __init__(self, url: str, *, prefix: str = "agent-platform") -> None:
        try:
            from redis.asyncio import Redis
        except ImportError as exc:  # pragma: no cover - dependency is deployment optional
            raise RuntimeError("redis support requires the 'distributed' dependency extra") from exc
        self._redis = Redis.from_url(url, decode_responses=True)
        self.prefix = prefix

    def _channel(self, topic: str, aggregate_id: UUID) -> str:
        return f"{self.prefix}:{topic}:{aggregate_id}"

    async def publish(self, topic: str, aggregate_id: UUID, payload: dict[str, Any]) -> None:
        await self._redis.publish(
            self._channel(topic, aggregate_id),
            json.dumps(payload, separators=(",", ":"), default=str),
        )

    async def wait(self, topic: str, aggregate_id: UUID, timeout: float) -> bool:
        pubsub = self._redis.pubsub()
        try:
            await pubsub.subscribe(self._channel(topic, aggregate_id))
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=timeout
            )
            return message is not None
        finally:
            await pubsub.aclose()

    async def close(self) -> None:
        await self._redis.aclose()


def create_event_notifier(redis_url: str | None) -> EventNotifier:
    return RedisEventNotifier(redis_url) if redis_url else InMemoryEventNotifier()

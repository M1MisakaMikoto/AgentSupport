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
    def __init__(self, url: str, *, prefix: str = "agentsupport") -> None:
        try:
            from redis.asyncio import Redis
        except ImportError as exc:  # pragma: no cover - dependency is deployment optional
            raise RuntimeError("redis support requires the 'postgres' dependency extra") from exc
        self._redis = Redis.from_url(url, decode_responses=True)
        self.prefix = prefix
        self._subscriptions: dict[tuple[str, UUID], Any] = {}
        self._sub_lock = asyncio.Lock()
        self._closed = False

    def _channel(self, topic: str, aggregate_id: UUID) -> str:
        return f"{self.prefix}:{topic}:{aggregate_id}"

    async def publish(self, topic: str, aggregate_id: UUID, payload: dict[str, Any]) -> None:
        await self._redis.publish(
            self._channel(topic, aggregate_id),
            json.dumps(payload, separators=(",", ":"), default=str),
        )

    async def _subscription(self, topic: str, aggregate_id: UUID) -> Any:
        """Reuse one pubsub subscription per channel.

        The previous implementation created a fresh ``pubsub()`` +
        ``subscribe`` + close cycle for every ``wait`` call. Besides the
        connection/SUBSCRIBE churn, the very first ``get_message`` with
        ``ignore_subscribe_messages=True`` consumed and discarded the
        subscribe confirmation and returned immediately, so a message
        published right after subscribing was silently lost (the SSE stream
        only survived thanks to database polling). We drain the confirmation
        once at subscribe time and reuse the subscription afterwards.
        """

        key = (topic, aggregate_id)
        async with self._sub_lock:
            if self._closed:
                raise RuntimeError("RedisEventNotifier is closed")
            pubsub = self._subscriptions.get(key)
            if pubsub is not None:
                return pubsub
            pubsub = self._redis.pubsub()
            await pubsub.subscribe(self._channel(topic, aggregate_id))
            # Drain the subscribe confirmation so it is not returned (and
            # discarded) by the first real wait.
            await pubsub.get_message(timeout=2.0)
            self._subscriptions[key] = pubsub
            return pubsub

    async def wait(self, topic: str, aggregate_id: UUID, timeout: float) -> bool:
        pubsub = await self._subscription(topic, aggregate_id)
        try:
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return False
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=remaining
                )
                if message is not None:
                    return True
        except Exception:
            self._subscriptions.pop((topic, aggregate_id), None)
            raise

    async def close(self) -> None:
        self._closed = True
        for pubsub in list(self._subscriptions.values()):
            await pubsub.aclose()
        self._subscriptions.clear()
        await self._redis.aclose()


def create_event_notifier(redis_url: str | None) -> EventNotifier:
    return RedisEventNotifier(redis_url) if redis_url else InMemoryEventNotifier()

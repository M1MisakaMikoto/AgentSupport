"""Unit tests for RedisEventNotifier subscription reuse and delivery."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from agentsupport.adapters.notification.notifier import RedisEventNotifier


class FakePubSub:
    def __init__(self) -> None:
        self.queue: list[dict] = []
        self.subscribe_calls = 0
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        del channel
        self.subscribe_calls += 1

    async def get_message(
        self,
        *,
        ignore_subscribe_messages: bool = True,
        timeout: float = 0,
    ) -> dict | None:
        del ignore_subscribe_messages, timeout
        if not self.queue:
            return None
        message = self.queue.pop(0)
        if message.get("type") in {
            "subscribe",
            "unsubscribe",
            "psubscribe",
            "punsubscribe",
        }:
            return None  # skipped by ignore_subscribe_messages, wait loop retries
        return message

    async def aclose(self) -> None:
        self.closed = True


class FakeRedis:
    def __init__(self) -> None:
        self.pubsubs: list[FakePubSub] = []
        self.published: list[tuple[str, str]] = []

    def pubsub(self) -> FakePubSub:
        pubsub = FakePubSub()
        self.pubsubs.append(pubsub)
        return pubsub

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return 1

    async def aclose(self) -> None:
        return None


def _confirmation(channel: str) -> dict:
    return {"type": "subscribe", "channel": channel, "data": 1}


def _message(channel: str) -> dict:
    return {"type": "message", "channel": channel, "data": "probe"}


@pytest.fixture
def notifier() -> tuple[RedisEventNotifier, FakeRedis]:
    instance = RedisEventNotifier("redis://localhost:6379/0")
    fake = FakeRedis()
    instance._redis = fake  # type: ignore[assignment]
    return instance, fake


@pytest.mark.asyncio
async def test_wait_reuses_one_subscription_per_channel(notifier):
    instance, fake = notifier
    aggregate: UUID = uuid4()
    channel = instance._channel("conversation.events", aggregate)

    # Create the subscription first; a late subscribe confirmation (as seen
    # after a reconnect) must be skipped by the wait loop, not returned.
    await instance._subscription("conversation.events", aggregate)
    fake.pubsubs[0].queue.append(_confirmation(channel))
    fake.pubsubs[0].queue.append(_message(channel))
    assert await instance.wait("conversation.events", aggregate, timeout=1.0) is True

    # Later waits reuse the same subscription (one pubsub, one SUBSCRIBE).
    for _ in range(4):
        fake.pubsubs[0].queue.append(_message(channel))
        assert await instance.wait("conversation.events", aggregate, timeout=1.0) is True

    assert len(fake.pubsubs) == 1
    assert fake.pubsubs[0].subscribe_calls == 1
    await instance.close()


@pytest.mark.asyncio
async def test_wait_returns_false_on_timeout(notifier):
    instance, _fake = notifier
    aggregate: UUID = uuid4()
    assert await instance.wait("conversation.events", aggregate, timeout=0.01) is False
    await instance.close()


@pytest.mark.asyncio
async def test_publish_payload(notifier):
    instance, fake = notifier
    aggregate: UUID = uuid4()
    await instance.publish("conversation.events", aggregate, {"probe": 1})
    channel, payload = fake.published[-1]
    assert channel == instance._channel("conversation.events", aggregate)
    assert '"probe":1' in payload
    await instance.close()

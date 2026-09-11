"""A retried model call must reset the stream instead of appending to it."""

from __future__ import annotations

from session_runner.adapters.trae import install_retry_delta_guard


class _AnthropicLikeClient:
    """Mimics the vendored client: ``chat()`` drives one or more stream attempts."""

    def __init__(self, attempts: int = 1) -> None:
        self.attempts = attempts
        self.stream_calls = 0

    def chat(self, *_args, **_kwargs) -> str:
        for index in range(self.attempts):
            try:
                return self._create_anthropic_response_stream()
            except RuntimeError:
                if index == self.attempts - 1:
                    raise
        return ""

    def _create_anthropic_response_stream(self, *_args, **_kwargs) -> str:
        self.stream_calls += 1
        if self.stream_calls == 1 and self.attempts > 1:
            raise RuntimeError("The read operation timed out")
        return "ok"


def _collect():
    emitted: list[tuple[str, dict]] = []
    return emitted, (lambda kind, payload: emitted.append((kind, payload)))


def test_first_attempt_does_not_reset():
    emitted, emit = _collect()
    client = _AnthropicLikeClient(attempts=1)

    assert install_retry_delta_guard(client, emit) is True
    client.chat()

    assert emitted == []


def test_retry_emits_a_reset():
    emitted, emit = _collect()
    client = _AnthropicLikeClient(attempts=2)
    install_retry_delta_guard(client, emit)

    client.chat()

    assert [kind for kind, _ in emitted] == ["message.reset"]
    assert emitted[0][1]["attempt"] == 2
    assert emitted[0][1]["reason"] == "stream_retry"


def test_counter_resets_between_calls():
    emitted, emit = _collect()
    client = _AnthropicLikeClient(attempts=1)
    install_retry_delta_guard(client, emit)

    client.chat()
    client.chat()

    assert emitted == []  # two ordinary calls are not retries


def test_install_is_idempotent():
    emitted, emit = _collect()
    client = _AnthropicLikeClient(attempts=2)

    assert install_retry_delta_guard(client, emit) is True
    assert install_retry_delta_guard(client, emit) is False

    client.chat()

    assert len(emitted) == 1


def test_non_anthropic_client_is_left_alone():
    class _OpenAiLike:
        def chat(self, *_args, **_kwargs) -> str:
            return "ok"

    assert install_retry_delta_guard(_OpenAiLike(), lambda kind, payload: None) is False

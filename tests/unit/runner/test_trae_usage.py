"""Runner-side model usage capture: payload normalization and metrics."""

from types import SimpleNamespace

from session_runner.adapters.trae import _usage_payload
from session_runner.metrics import record_llm_usage, render_metrics


def test_usage_payload_normalizes_llm_usage():
    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        cache_creation_input_tokens=2,
        cache_read_input_tokens=3,
        reasoning_tokens=1,
    )
    assert _usage_payload(usage) == {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_creation_input_tokens": 2,
        "cache_read_input_tokens": 3,
        "reasoning_tokens": 1,
    }


def test_usage_payload_accepts_none_and_partial_objects():
    assert _usage_payload(None) is None
    assert _usage_payload(SimpleNamespace(input_tokens=4)) == {
        "input_tokens": 4,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "reasoning_tokens": 0,
    }


def test_record_llm_usage_exports_prometheus_counters():
    record_llm_usage(
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_creation_input_tokens": 2,
            "cache_read_input_tokens": 3,
            "reasoning_tokens": 0,
        }
    )
    text = render_metrics()[0].decode()
    lines = {
        line.split(" ", 1)[0]: float(line.split(" ", 1)[1])
        for line in text.splitlines()
        if line.startswith("session_runner_llm_tokens_total{")
    }
    assert lines.get('session_runner_llm_tokens_total{kind="input_tokens"}', 0) >= 10.0
    assert lines.get('session_runner_llm_tokens_total{kind="output_tokens"}', 0) >= 5.0
    assert (
        lines.get('session_runner_llm_tokens_total{kind="cache_read_input_tokens"}', 0)
        >= 3.0
    )

"""Settings env-var reading for evaluation-layer knobs."""

from agentsupport.bootstrap.settings import Settings


def test_eval_case_timeout_reads_from_env(monkeypatch):
    monkeypatch.setenv("AGENTSUPPORT_EVAL_CASE_TIMEOUT_SECONDS", "123")
    assert Settings().eval_case_timeout_seconds == 123


def test_eval_case_timeout_default(monkeypatch):
    monkeypatch.delenv("AGENTSUPPORT_EVAL_CASE_TIMEOUT_SECONDS", raising=False)
    assert Settings().eval_case_timeout_seconds == 1800

"""SSE Last-Event-ID resume cursor resolution."""

from agentsupport.serving.http.routes.events import _resume_seq


def test_resume_seq_defaults_to_zero():
    assert _resume_seq(None, None) == 0


def test_resume_seq_uses_last_event_id():
    assert _resume_seq(None, "7") == 7


def test_resume_seq_ignores_malformed_last_event_id():
    assert _resume_seq(None, "abc") == 0


def test_resume_seq_query_param_wins():
    assert _resume_seq(3, "7") == 3

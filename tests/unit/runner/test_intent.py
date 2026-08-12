"""Unit tests for the social-message intent guard."""

import pytest

from session_runner.intent import classify_social, greeting_reply


@pytest.mark.parametrize(
    "task",
    [
        "hello",
        "Hello!",
        "hi",
        "hey there",
        "你好",
        "您好！",
        "早上好",
        "在吗",
        "thanks",
        "thank you",
        "谢谢",
        "bye",
        "再见",
    ],
)
def test_social_messages_are_classified(task):
    assert classify_social(task) is True


@pytest.mark.parametrize(
    "task",
    [
        "",
        "hello, please refactor main.py",
        "你好，帮我看看这个项目的结构",
        "fix the failing test",
        "在吗？帮我修一下登录报错",
        "thanks, now fix the bug",
        "分析项目并输出说明文档",
    ],
)
def test_substantive_tasks_are_not_intercepted(task):
    assert classify_social(task) is False


def test_greeting_reply_has_default(monkeypatch):
    monkeypatch.delenv("SESSION_RUNNER_GREETING_REPLY", raising=False)
    assert "AgentSupport" in greeting_reply()
    monkeypatch.setenv("SESSION_RUNNER_GREETING_REPLY", "你好，请问需要什么帮助？")
    assert greeting_reply() == "你好，请问需要什么帮助？"

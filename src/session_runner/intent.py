"""Code-level intent guard: short-circuit social/greeting tasks before the agent.

The vendored Trae system prompt already instructs the model not to call tools
for greetings, but instruction-following varies by model. This module makes the
zero-tool guarantee at the Runner boundary for clearly social messages.
"""

from __future__ import annotations

import os
import re

_DEFAULT_REPLY = (
    "你好！我是 AgentSupport 上的编码智能体，可以帮你分析代码、修复问题或编写测试。"
    "有什么具体任务吗？"
)

# Whole-message patterns: a bare "hello" is intercepted, while
# "hello, please refactor main.py" still goes to the agent.
_PATTERNS = (
    re.compile(r"^(hello|hi|hey|hey there|yo|howdy)[\s,.!?。，！？]*$", re.IGNORECASE),
    re.compile(r"^(你好|您好|嗨|哈喽|哈啰|早上好|下午好|晚上好|在吗|在不在)[\s,!?。，！？]*$"),
    re.compile(r"^(thanks|thank you|thx)[\s,.!?。，！？]*$", re.IGNORECASE),
    re.compile(r"^(谢谢|感谢)[\s,!?。，！？]*$"),
    re.compile(r"^(bye|goodbye|see you)[\s,.!?。，！？]*$", re.IGNORECASE),
    re.compile(r"^(再见|拜拜)[\s,!?。，！？]*$"),
)


def classify_social(task: str) -> bool:
    text = (task or "").strip()
    if not text:
        return False
    return any(pattern.match(text) for pattern in _PATTERNS)


def greeting_reply() -> str:
    return os.getenv("SESSION_RUNNER_GREETING_REPLY", _DEFAULT_REPLY).strip() or _DEFAULT_REPLY


__all__ = ["classify_social", "greeting_reply"]

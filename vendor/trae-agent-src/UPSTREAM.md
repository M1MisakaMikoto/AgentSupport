# Trae Agent upstream

- Project: `trae-agent`
- Version: `0.1.0`
- Source snapshot: `E:\PythonProject\WorkBranch\.tools\trae-agent-src`
- License: MIT, retained in `LICENSE`

This directory is a controlled source snapshot used by the Session Runner image. Platform code
does not modify Trae's agent loop directly; integration is performed through the documented
`BaseAgent._tool_caller` boundary.

## Local patches

- `trae_agent/utils/llm_clients/anthropic_client.py`: preserve `thinking` content blocks in the
  recorded assistant message so extended-thinking tool calls echo the blocks (with signatures)
  back to the API. Anthropic-compatible providers (e.g. DeepSeek) reject the follow-up message
  otherwise. Re-apply when syncing a newer snapshot.
- `trae_agent/agent/trae_agent.py`: support an optional `_system_prompt` override so hosts such as
  AgentSupport can plug in a custom system prompt, and fall back to a neutral `[User request]`
  user-message template when no issue text is provided (instead of always framing every message
  as a GitHub issue). Re-apply when syncing a newer snapshot.
